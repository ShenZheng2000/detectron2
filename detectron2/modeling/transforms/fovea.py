import torch
import torch.nn.functional as F
from .invert_grid import invert_grid
from .reblur import is_out_of_bounds, get_vanising_points
from detectron2.data.transforms import ResizeTransform, HFlipTransform, NoOpTransform
from torchvision import utils as vutils
import sys

def unwarp_bboxes(bboxes, grid, output_shape):
    """Unwarps a tensor of bboxes of shape (n, 4) or (n, 5) according to the grid \
    of shape (h, w, 2) used to warp the corresponding image and the \
    output_shape (H, W, ...)."""
    bboxes = bboxes.clone()
    # image map of unwarped (x,y) coordinates
    img = grid.permute(2, 0, 1).unsqueeze(0)

    warped_height, warped_width = grid.shape[0:2]

    xgrid = 2 * (bboxes[:, 0:4:2] / float(warped_width)) - 1
    ygrid = 2 * (bboxes[:, 1:4:2] / float(warped_height)) - 1
    grid = torch.stack((xgrid, ygrid), dim=2).unsqueeze(0)

    # warped_bboxes has shape (2, num_bboxes, 2)
    warped_bboxes = F.grid_sample(
        img, grid, align_corners=True, padding_mode="border").squeeze(0)

    bboxes[:, 0:4:2] = (warped_bboxes[0] + 1) / 2 * output_shape[1]
    bboxes[:, 1:4:2] = (warped_bboxes[1] + 1) / 2 * output_shape[0]

    return bboxes


def warp_bboxes(bboxes, grid, separable=True):

    size = grid.shape
    
    inverse_grid_shape = torch.Size((size[0], size[3], size[1], size[2]))
    inverse_grid = invert_grid(grid, inverse_grid_shape, separable) # [1, 2, 720, 1280]

    bboxes = unwarp_bboxes(bboxes, inverse_grid.squeeze(0), size[1:3]) #output_shape[1:3]=[720, 1280]

    return bboxes


def simple_test(grid_net, imgs, vanishing_point):
    """Test function without test time augmentation.
    Args:
        grid_net (CuboidGlobalKDEGrid): An instance of CuboidGlobalKDEGrid.
        imgs (list[torch.Tensor]): List of multiple images
        img_metas (list[dict]): List of image information.
    Returns:
        list[list[np.ndarray]]: BBox results of each image and classes.
            The outer list corresponds to each image. The inner list
            corresponds to each class.
    """
    if len(imgs.shape) == 3:
        imgs = imgs.unsqueeze(0)

    imgs = torch.stack(tuple(imgs), dim=0)
    # print("imgs shape", imgs.shape)

    grid = grid_net(imgs, vanishing_point)
    # print("grid shape", grid.shape)

    warped_imgs = F.grid_sample(imgs, grid, align_corners=True)

    return grid, warped_imgs



def make_warp_aug(img, ins, vanishing_point, grid_net, use_ins=True):

    # read image
    img = img.float()
    device = img.device
    my_shape = img.shape[-2:]
    imgs = img.unsqueeze(0) 

    if use_ins:

        # read bboxes
        bboxes = ins.gt_boxes.tensor
        bboxes = bboxes.to(device)

        # warp image
        grid, warped_imgs = simple_test(grid_net, imgs, vanishing_point)

        # warp bboxes
        warped_bboxes = warp_bboxes(bboxes, grid, separable=True)

        # # NOTE: hardcode for debug only. Delete later
        # warped_bboxes = unwarp_bboxes(warped_bboxes, grid.squeeze(0), [600, 1067])

        # update ins
        ins.gt_boxes.tensor = warped_bboxes

        return warped_imgs, ins, grid
    
    else:
        # warp image
        grid, warped_imgs = simple_test(grid_net, imgs, vanishing_point)

        return warped_imgs, ins, grid
    


def apply_warp_aug(img, ins, vanishing_point, warp_aug=False, 
                    warp_aug_lzu=False, grid_net=None, keep_size=True):
    # print(f"img is {img.shape}") # [3, 600, 1067]
    grid = None

    img_height, img_width = img.shape[-2:]

    # if is_out_of_bounds(vanishing_point, img_width, img_height):
    #     print("HERE!!!")
    #     return img, ins, grid
    if warp_aug:
        img, ins, grid = make_warp_aug(img, ins, vanishing_point, grid_net, use_ins=True)
    elif warp_aug_lzu:
        img, ins, grid = make_warp_aug(img, ins, vanishing_point, grid_net, use_ins=False)

    # reshape 4d to 3d
    if (len(img.shape) == 4) and keep_size:
        img = img.squeeze(0) 

    return img, ins, grid



def apply_unwarp(warped_x, grid, keep_size=True):
    if (len(warped_x.shape) == 3) and keep_size:
        warped_x = warped_x.unsqueeze(0)

    # print(f'warped_x is {warped_x.shape} grid is {grid.shape}') # [1, 3, 600, 1067], [1, 600, 1067, 2]

    # Compute inverse_grid
    inverse_grid = invert_grid(grid, warped_x.shape, separable=True)[0:1]

    # Expand inverse_grid to match batch size
    B = warped_x.shape[0]
    inverse_grid = inverse_grid.expand(B, -1, -1, -1)

    # Perform unzoom
    unwarped_x = F.grid_sample(
        warped_x, inverse_grid, mode='bilinear',
        align_corners=True, padding_mode='zeros'
    )
    # print("unwarped_x shape", unwarped_x.shape) # [1, 3, 600, 1067]

    if (len(unwarped_x.shape) == 4) and keep_size:
        unwarped_x = unwarped_x.squeeze(0)

    # print("unwarped_x shape", unwarped_x.shape) # [3, 600, 1067]

    # print(f"unwarped_x min {unwarped_x.min()} max {unwarped_x.max()}") # [0, 255]

    return unwarped_x



def extract_ratio_and_flip(transform_list):
    for transform in transform_list:
        if isinstance(transform, ResizeTransform):
            ratio = transform.new_h / transform.h
        elif isinstance(transform, (HFlipTransform, NoOpTransform)):
            flip = transform
    return ratio, flip



def process_and_update_features(batched_inputs, images, warp_aug_lzu, vp_dict, grid_net, backbone):
    features = None
    if warp_aug_lzu:
        # Preprocessing
        vanishing_points = [
            get_vanising_points(
                sample['file_name'], 
                vp_dict, 
                # *extract_ratio_and_flip(sample['transform']) # NOTE: hardcode remove this for now
            ) for sample in batched_inputs
        ]

        # Apply warping
        warped_images, _, grids = zip(*[
            apply_warp_aug(image, None, vp, False, warp_aug_lzu, grid_net) 
            for image, vp in zip(images.tensor, vanishing_points)
        ])
        warped_images = torch.stack(warped_images)

        # # # NOTE: debug visualization
        # for i, img in enumerate(warped_images):
        #     # Save the image
        #     vutils.save_image(img, f'warped_image_{i}.jpg', normalize=True)        

        # sys.exit(1)

        # Call the backbone
        features = backbone(warped_images)

        # Apply unwarping
        feature_key = next(iter(features))
        unwarped_features = torch.stack([
            apply_unwarp(feature, grid)
            for feature, grid in zip(features[feature_key], grids)
        ])

        # Replace the original features with unwarped ones
        features[feature_key] = unwarped_features

    return features
