import os
from typing import cast, Literal

import torch
from PIL import Image
from torchvision import transforms
from scipy.ndimage import binary_dilation, binary_erosion
import numpy as np
from safetensors.torch import load_file

from modules.modelloader import load_file_from_url

from birefnet.image_proc import refine_foreground
from birefnet.models.birefnet import BiRefNet

try:
    from modules.paths_internal import models_path
except:
    try:
        from modules.paths import models_path
    except:
        models_path = os.path.abspath("models")


usage_to_weights_file = {
    "General": "BiRefNet",
    "General-HR": "BiRefNet_HR",
    "General-Lite": "BiRefNet_T",
    "General-Lite-2K": "BiRefNet_lite-2K",
    "Portrait": "BiRefNet-portrait",
    "Matting": "BiRefNet-matting",
    "Matting-HR": "BiRefNet_HR-matting",
    "Matting-Lite": "BiRefNet_lite-matting",
    "Anime-Lite": "BiRefNet_lite-anime",
    "Dynamic": "BiRefNet_dynamic",
    "DIS": "BiRefNet-DIS5K",
    "HRSOD": "BiRefNet-HRSOD",
    "COD": "BiRefNet-COD",
    "DIS-TR_TEs": "BiRefNet-DIS5K-TR_TEs",
}

BiRefNetModelName = Literal[
    "General",
    "General-HR",
    "General-Lite",
    "General-Lite-2K",
    "Portrait",
    "Matting",
    "Matting-HR",
    "Matting-Lite",
    "Anime-Lite",
    "Dynamic",
    "DIS",
    "HRSOD",
    "COD",
    "DIS-TR_TEs",
]


def get_model_path(model_name: BiRefNetModelName):
    return os.path.join(models_path, "birefnet", f"{model_name}.safetensors")


def download_models(model_root, model_urls):
    if not os.path.exists(model_root):
        os.makedirs(model_root, exist_ok=True)

    for local_file, url in model_urls:
        local_path = os.path.join(model_root, local_file)
        if not os.path.exists(local_path):
            load_file_from_url(url, model_dir=model_root, file_name=local_file)


def download_birefnet_model(model_name: BiRefNetModelName):
    """
    Downloading birefnet model from huggingface.
    """
    model_root = os.path.join(models_path, "birefnet")
    model_urls = (
        (
            f"{model_name}.safetensors",
            f"https://huggingface.co/ZhengPeng7/{usage_to_weights_file[model_name]}/resolve/main/model.safetensors",
        ),
    )
    download_models(model_root, model_urls)


class ImagePreprocessor:
    def __init__(self) -> None:
        self.transform_image: transforms.Compose = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )

    def proc(self, image: Image.Image) -> torch.Tensor:
        image_tf = cast(torch.Tensor, self.transform_image(image))
        return image_tf


class BiRefNetPipeline(object):
    def __init__(
        self,
        model_name: BiRefNetModelName = "General",
        device_id: int = 0,
        flag_force_cpu: bool = False,
        use_fp16: bool = True,
    ):
        self.model_name = model_name
        self.device_id = device_id
        self.flag_force_cpu = flag_force_cpu
        self.use_fp16 = use_fp16

        if self.flag_force_cpu:
            self.device = "cpu"
        else:
            try:
                if torch.backends.mps.is_available():
                    self.device = "mps"
                elif torch.cuda.is_available():
                    self.device = "cuda:" + str(self.device_id)
                else:
                    self.device = "cpu"
            except:
                self.device = "cpu"

        download_birefnet_model(self.model_name)

        weight_path = get_model_path(self.model_name)

        state_dict = load_file(weight_path, device=self.device)

        bb_index = 3 if "Lite" in model_name else 6

        self.birefnet = BiRefNet(bb_pretrained=False, bb_index=bb_index)
        self.birefnet.load_state_dict(state_dict)
        self.birefnet.to(self.device)
        self.birefnet.eval()
        if self.use_fp16:
            self.birefnet.half()

    def dilate_mask(self, mask, dilation_amt: int):
        dilation_amt_abs = abs(dilation_amt)
        x, y = np.meshgrid(np.arange(dilation_amt_abs), np.arange(dilation_amt_abs))
        center = dilation_amt_abs // 2
        if dilation_amt < 0:
            dilation_kernel = (
                (x - center) ** 2 + (y - center) ** 2 <= center**2
            ).astype(np.uint8)
            dilated_binary_img = binary_erosion(mask, dilation_kernel)
        else:
            dilation_kernel = (
                (x - center) ** 2 + (y - center) ** 2 <= center**2
            ).astype(np.uint8)
            dilated_binary_img = binary_dilation(mask, dilation_kernel)
        return cast(np.ndarray, dilated_binary_img)

    def get_edge_mask(self, mask: Image.Image, mask_width: int):
        dilation_amt = mask_width // 2
        dilate_binary_img = self.dilate_mask(mask, dilation_amt)
        erode_binary_img = self.dilate_mask(mask, -dilation_amt)
        binary_img = dilate_binary_img ^ erode_binary_img
        return Image.fromarray(binary_img.astype(np.uint8) * 255)

    def process(
        self,
        image: Image.Image,
        resolution: str,
        return_mask: bool,
        return_foreground: bool,
        return_edge_mask: bool,
        edge_mask_width: int,
    ):
        if not return_mask and not return_foreground and not return_edge_mask:
            return None, None, None

        image_resolution = (
            f"{image.width}x{image.height}" if resolution == "" else resolution
        )
        image_resolution = [
            int(int(reso) // 32 * 32) for reso in image_resolution.strip().split("x")
        ]
        image_resolution = cast(tuple[int, int], tuple(image_resolution))

        image_pil = image.resize(image_resolution)

        image_preprocessor = ImagePreprocessor()
        image_proc = image_preprocessor.proc(image_pil)
        image_proc = image_proc.unsqueeze(0)
        if self.use_fp16:
            image_proc = image_proc.half()

        with torch.no_grad():
            scaled_pred_tensor = self.birefnet(image_proc.to(self.device))[-1].sigmoid()

        pred = scaled_pred_tensor[0].squeeze().cpu()

        mask = transforms.ToPILImage()(pred).resize(image.size)

        if return_foreground:
            foreground = refine_foreground(image, mask)
            foreground.putalpha(mask)
        else:
            foreground = None
        if return_edge_mask:
            edge_mask = self.get_edge_mask(mask, edge_mask_width)
        else:
            edge_mask = None

        if not return_mask:
            mask = None

        return mask, foreground, edge_mask
