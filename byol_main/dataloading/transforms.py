import torch
import torchvision.transforms as T
import torchvision
import numpy as np
import albumentations as A
import astroaugmentations as AA
from torch import nn
from PIL import ImageFilter
from albumentations.pytorch import ToTensorV2

from byol_main.paths import Path_Handler
import cv2

from pytorch_galaxy_datasets import galaxy_datamodule


class Circle_Crop(torch.nn.Module):
    """
    PyTorch transform to set all values outside largest possible circle that fits inside image to 0.
    """

    def __init__(self):
        super().__init__()

    def forward(self, img):
        """
        Returns an image with all values outside the central circle bounded by image edge masked to 0.

        !!! Support for multiple channels not implemented yet !!!
        """
        H, W, C = img.shape[-1], img.shape[-2], img.shape[-3]
        assert H == W
        x = torch.arange(W, dtype=torch.float).repeat(H, 1)
        x = (x - 74.5) / 74.5
        y = torch.transpose(x, 0, 1)
        r = torch.sqrt(torch.pow(x, 2) + torch.pow(y, 2))
        r = r / torch.max(r)
        r[r < 0.5] = -1
        r[r == 0.5] = -1
        r[r != -1] = 0
        r = torch.pow(r, 2).view(C, H, W)
        assert r.shape == img.shape
        img = torch.mul(r, img)
        return img


class MultiView(nn.Module):
    def __init__(self, config, n_views=2, mu=(0,), sig=(1,)):
        super().__init__()
        self.config = config

        # Define a view
        self.view = self._view()  # creates a callable transform
        # self.normalize = T.Normalize(mu, sig)
        self.normalize = lambda x: x  # TODO temporarily disable normalisation (see also SimpleView)
        self.n_views = n_views

    def __call__(self, x):
        if self.n_views > 1:
            views = []
            for i in np.arange(self.n_views):
                view = self.view(x)
                view = self.normalize(view)
                views.append(view)
            return views
        else:
            view = self.normalize(self.view(x))
            return view

    def _view(self):
        if self.config["dataset"] == "rgz":
            return _rgz_view(self.config)

        elif self.config["dataset"] in ["imagenette", "stl10", "cifar10"]:
            return _simclr_view(self.config)

        elif self.config["dataset"] == "gzmnist":
            return _gzmnist_view(self.config)

        # TODO could get ugly
        elif self.config["dataset"] in ["gz2", "decals_dr5", "legs", "rings", "legs_and_rings"]:
            return _gz2_view(self.config)  # now badly named TODO

        elif self.config["dataset"] == "mixed":
            # return _zoobot_default_view(self.config)
            return _gz2_view(self.config)  # now badly named TODO

        else:
            raise ValueError(self.config["dataset"])

    def update_normalization(self, mu, sig):
        self.normalize = T.Normalize(mu, sig)


class SimpleView(nn.Module):
    def __init__(self, config, mu=(0,), sig=(1,)):
        super().__init__()
        self.config = config

        augs = []
        # if config['dataset'] == 'gz2':  # is a tensor, needs to be a PIL to later call T.ToTensor
        #     augs.append(T.ToPILImage())

        if config["data"]["rotate"]:
            augs.append(T.RandomRotation(180))

        augs.append(T.Resize(config["data"]["input_height"]))

        if config["augmentations"]["center_crop_size"]:
            augs.append(T.CenterCrop(config["augmentations"]["center_crop_size"]))

        augs.append(T.ToTensor())
        self.view = T.Compose(augs)

        # self.normalize = T.Normalize(mu, sig)
        self.normalize = lambda x: x  # TODO temporarily disable normalisation (see also MultiView)

    def __call__(self, x):
        # Use rotation if training
        x = self.view(x)
        x = self.normalize(x)
        return x

    def update_normalization(self, mu, sig):
        self.normalize = T.Normalize(mu, sig)


class ReduceView(nn.Module):
    def __init__(self, encoder, config, train=True):
        super().__init__()

        augs = []

        if config["data"]["rotate"] and train:
            augs.append(T.RandomRotation(180))
            augs.append(T.RandomHorizontalFlip())

        augs.append(T.Resize(config["data"]["input_height"]))

        augs.append(T.ToTensor())

        self.view = T.Compose(augs)

        self.pre_normalize = T.Normalize(config["data"]["mu"], config["data"]["sig"])
        self.encoder = encoder
        self.normalize = T.Normalize(0, 1)

    def __call__(self, x):
        x = self.view(x)
        x = self.pre_normalize(x)
        x = torch.unsqueeze(x, 0)
        x = self.encoder(x).view(-1, 1, 1)
        x = self.normalize(x)
        return x

    def update_normalization(self, mu, sig):
        self.normalize = T.Normalize(mu, sig)


class SupervisedView(nn.Module):
    def __init__(self, config, mu=(0,), sig=(1,)):
        super().__init__()
        self.config = config

        augs = []

        if config["data"]["rotate"]:
            augs.append(T.RandomRotation(180))

        augs.append(T.Resize(config["data"]["input_height"]))

        if config["center_crop_size"]:
            augs.append(T.CenterCrop(config["center_crop_size"]))
        augs.append(T.ToTensor())
        self.view = T.Compose(augs)

        self.normalize = T.Normalize(mu, sig)

    def __call__(self, x):
        # Use rotation if training
        x = self.view(x)
        x = self.normalize(x)
        return x

    def update_normalization(self, mu, sig):
        self.normalize = T.Normalize(mu, sig)


def _simclr_view(config):
    # Returns a SIMCLR view

    s = config["augmentations"]["s"]
    input_height = config["data"]["input_height"]

    color_jitter = T.ColorJitter(0.8 * s, 0.8 * s, 0.8 * s, 0.2 * s)

    # Gaussian blurring, kernel 10% of image size (SimCLR paper)
    blur_kernel = _blur_kernel(input_height)
    blur = LightlyGaussianBlur(blur_kernel, prob=0.5)

    # Define a view
    view = T.Compose(
        [
            T.RandomResizedCrop(input_height, scale=(0.08, 1)),
            T.RandomHorizontalFlip(),
            # T.RandomVerticalFlip(),
            T.RandomApply([color_jitter], p=0.8),
            T.RandomGrayscale(p=0.2),
            blur,
            T.ToTensor(),
        ]
    )

    return view


def _gzmnist_view(config):
    s = config["augmentations"]["s"]
    input_height = config["data"]["input_height"]  # TODO adjust for 128pix

    # Gaussian blurring, kernel 10% of image size (SimCLR paper)
    p_blur = config["p_blur"]
    blur_kernel = _blur_kernel(input_height)
    # blur_sig = config["blur_sig"]
    # blur = SIMCLR_GaussianBlur(blur_kernel, p=p_blur, min=blur_sig[0], max=blur_sig[1])
    blur = LightlyGaussianBlur(blur_kernel, prob=p_blur)

    # Color augs
    color_jitter = T.ColorJitter(0.8 * s, 0.8 * s, 0.8 * s, 0.2 * s)
    p_grayscale = config["p_grayscale"]

    # Cropping
    random_crop = config["random_crop_scale"]

    # Define a view
    view = T.Compose(
        [
            T.RandomRotation(180),
            T.RandomResizedCrop(input_height, scale=random_crop),
            T.RandomHorizontalFlip(),
            T.RandomVerticalFlip(),
            T.RandomApply([color_jitter], p=0.8),
            T.RandomGrayscale(p=p_grayscale),
            blur,
            T.ToTensor(),
        ]
    )

    return view


def _gz2_view(config):
    # currently the same as gzmnist except for the resizing and ToPIL transform
    s = config["augmentations"]["s"]
    # images are loaded from disk at whatever size (424, in practice). Not a parameter
    # images are then downscaled to `downscale_height`, calculated according to the final desired input size * "precrop_size_ratio"
    # finally, images are random-cropped to input_height and sent to model
    # e.g. image loaded at 424, resized to downscale_height=300 (424 * precrop_size_ratio=0.75), then random-cropped to input_height=224
    precrop_size_ratio = config["data"]["precrop_size_ratio"]
    assert (
        precrop_size_ratio >= 1.0
    )  # >1 implies image is still bigger than model input height after resizing, leaving room to random-crop
    input_height = config["data"]["input_height"]  # i.e. after RandomResizedCrop, when input to model

    downscale_height = int(min(424, input_height * precrop_size_ratio))

    # Gaussian blurring, kernel 10% of image size (SimCLR paper)
    p_blur = config["p_blur"]
    blur_kernel = _blur_kernel(input_height)
    blur = LightlyGaussianBlur(blur_kernel, prob=p_blur)

    # Color augs
    color_jitter = T.ColorJitter(0.8 * s, 0.8 * s, 0.8 * s, 0.2 * s)
    p_grayscale = config["p_grayscale"]

    # Define a view
    view = T.Compose(
        [
            # the GZ2 dataset yields tensors (may change this)
            # T.ToPILImage(),  # the blur transform requires a PIL image
            T.Resize(downscale_height),
            T.RandomRotation(180),
            T.RandomResizedCrop(
                input_height, scale=config["random_crop_scale"], ratio=config["random_crop_ratio"]
            ),
            T.RandomHorizontalFlip(),
            T.RandomVerticalFlip(),
            T.RandomApply([color_jitter], p=0.8),
            T.RandomGrayscale(p=p_grayscale),
            blur,  # no, need all the resolution we can get
            T.ToTensor(),  # the model requires a tensor
        ]
    )

    return view


def _zoobot_default_view(config):
    transforms = galaxy_datamodule.default_torchvision_transforms(
        greyscale=False,
        resize_size=config["data"]["input_height"],
        crop_scale_bounds=(0.7, 0.8),
        crop_ratio_bounds=(0.9, 1.1),
    )
    view = T.Compose(transforms)
    return view


def _rgz_view(config):
    if config["augmentations"]["type"] == "simclr":

        # Gaussian blurring
        # blur_kernel = config["blur_kernel"]
        # input_height = config["center_crop_size"]
        # blur_kernel = _blur_kernel(input_height)
        blur_kernel = 3
        # blur_sig = config["blur_sig"]
        p_blur = config["augmentations"]["p_blur"]
        blur = LightlyGaussianBlur(blur_kernel, prob=p_blur)
        # blur = T.GaussianBlur(blur_kernel, sigma=blur_sig)

        # Cropping
        center_crop = config["augmentations"]["center_crop_size"]
        random_crop = config["augmentations"]["random_crop_scale"]

        # Color jitter
        s = config["augmentations"]["s"]
        color_jitter = T.ColorJitter(0.8 * s, 0.8 * s, 0.8 * s, 0)

        # Define a view
        view = T.Compose(
            [
                T.RandomRotation(180),
                T.CenterCrop(center_crop),
                T.RandomResizedCrop(center_crop, scale=random_crop),
                T.RandomHorizontalFlip(),
                T.RandomVerticalFlip(),
                T.RandomApply([color_jitter], p=0.8),
                blur,
                T.ToTensor(),
            ]
        )

        return view

    elif config["augmentations"]["type"] == "astroaug":

        paths = Path_Handler()
        path_dict = paths._dict()
        kernel_path = path_dict["main"] / "dataloading" / "FIRST_kernel.npy"
        kernel = np.load(kernel_path)

        p = config["augmentations"]["p_aug"]

        # Cropping
        center_crop = config["augmentations"]["center_crop_size"]
        # random_crop = config["random_crop_scale"]

        augs = [
            # Change source perspective
            A.Lambda(
                name="Superpixel spectral index change",
                image=AA.radio.SpectralIndex(
                    mean=-0.8, std=0.2, super_pixels=True, n_segments=100, seed=None
                ),
                p=p,
            ),  # With segmentation
            A.Lambda(
                name="Brightness perspective distortion",
                image=AA.BrightnessGradient(limits=(0.0, 1.0)),
                p=p,
            ),  # No noise
            A.ElasticTransform(  # Elastically transform the source
                sigma=100, alpha_affine=25, interpolation=1, border_mode=1, value=0, p=p
            ),
            A.ShiftScaleRotate(
                shift_limit=0.1,
                scale_limit=0.1,
                rotate_limit=90,
                interpolation=2,
                border_mode=0,
                value=0,
                p=p,
            ),
            A.VerticalFlip(p=0.5),
            # Change properties of noise / imaging artefacts
            A.Lambda(
                name="Spectral index change of whole image",
                image=AA.radio.SpectralIndex(mean=-0.8, std=0.2, seed=None),
                p=p,
            ),  # Across the whole image
            A.Emboss(
                alpha=(0.2, 0.5), strength=(0.2, 0.5), p=p
            ),  # Quick emulation of incorrect w-kernels # Doesnt force the maxima to 1
            A.Lambda(
                name="Dirty beam convlolution",
                image=AA.radio.CustomKernelConvolution(
                    kernel=kernel, rfi_dropout=0.4, psf_radius=1.3, sidelobe_scaling=1, mode="sum"
                ),
                p=p,
            ),  # Add sidelobes
            A.Lambda(
                name="Brightness perspective distortion",
                image=AA.BrightnessGradient(limits=(0.0, 1), primary_beam=True, noise=0.01),
                p=p,
            ),  # Gaussian Noise and pb brightness scaling
            # Modelling based transforms
            A.ShiftScaleRotate(
                shift_limit=0.1,
                scale_limit=0.1,
                rotate_limit=180,
                interpolation=2,
                border_mode=0,
                value=0,
                p=p,
            ),
            A.CenterCrop(width=center_crop, height=center_crop, p=1),
            A.Lambda(
                name="Dirty beam convlolution",
                image=AA.MinMaxNormalize(mean=0.5, std=0.5),
                always_apply=True,
            ),
            ToTensorV2(),
        ]

        view = A.Compose(augs)

        return lambda img: view(image=np.array(img))["image"]


def _blur_kernel(input_height):
    blur_kernel = int(input_height * 0.1)
    if blur_kernel % 2 == 0:
        blur_kernel += 1
    return blur_kernel


class SIMCLR_GaussianBlur:
    """Taken from  https://github.com/PyTorchLightning/lightning-bolts/blob/2415b49a2b405693cd499e09162c89f807abbdc4/pl_bolts/models/self_supervised/simclr/transforms.py#L17"""

    # Implements Gaussian blur as described in the SimCLR paper
    def __init__(self, kernel_size, p=0.5, min=0.1, max=2.0):

        self.min = min
        self.max = max

        # kernel size is set to be 10% of the image height/width
        self.kernel_size = kernel_size
        self.p = p

    def __call__(self, sample):
        sample = np.array(sample)

        # blur the image with a 50% chance
        prob = np.random.random_sample()

        if prob < self.p:
            sigma = (self.max - self.min) * np.random.random_sample() + self.min
            sample = cv2.GaussianBlur(sample, (self.kernel_size, self.kernel_size), sigma)

        return sample


class LightlyGaussianBlur(object):
    """
    COPIED DIRECTLY FROM LIGHTLY https://github.com/lightly-ai/lightly/blob/master/lightly/transforms/gaussian_blur.py

    Implementation of random Gaussian blur.
    Utilizes the built-in ImageFilter method from PIL to apply a Gaussian
    blur to the input image with a certain probability. The blur is further
    randomized as the kernel size is chosen randomly around a mean specified
    by the user.
    Attributes:
        kernel_size:
            Mean kernel size for the Gaussian blur.
        prob:
            Probability with which the blur is applied.
        scale:
            Fraction of the kernel size which is used for upper and lower
            limits of the randomized kernel size.
    """

    def __init__(self, kernel_size: float, prob: float = 0.5, scale: float = 0.2):
        self.prob = prob
        self.scale = scale
        # limits for random kernel sizes
        self.min_size = (1 - scale) * kernel_size
        self.max_size = (1 + scale) * kernel_size
        self.kernel_size = kernel_size

    def __call__(self, sample):
        """Blurs the image with a given probability.
        Args:
            sample:
                PIL image to which blur will be applied.

        Returns:
            Blurred image or original image.
        """
        prob = np.random.random_sample()
        if prob < self.prob:
            # choose randomized kernel size
            kernel_size = np.random.normal(self.kernel_size, self.scale * self.kernel_size)
            kernel_size = max(self.min_size, kernel_size)
            kernel_size = min(self.max_size, kernel_size)
            radius = int(kernel_size / 2)
            # return blurred image
            return sample.filter(ImageFilter.GaussianBlur(radius=radius))
        # return original image
        return sample
