from pathlib import Path
from typing import Tuple, Union

import numpy as np
import torch
from PIL import Image, ImageDraw
from torchvision.transforms.functional import (_get_perspective_coeffs,
                                               to_tensor)

from .model import WPODNet


class Prediction:
    def __init__(self, image: Image.Image, bounds: np.ndarray, confidence: float):
        self.image = image
        self.bounds = bounds
        self.confidence = confidence

    def annotate(self, fp: Union[str, Path], outline: str = 'red', width: int = 3):
        canvas = self.image.copy()
        drawer = ImageDraw.Draw(canvas)
        drawer.polygon(
            [(x, y) for x, y in self.bounds],
            outline=outline,
            width=width
        )
        canvas.save(fp)

    def warp(self, fp: Union[str, Path], width: int = 208, height: int = 60):
        # Get the perspective matrix
        src_points = self.bounds.tolist()
        dst_points = [[0, 0], [width, 0], [width, height], [0, height]]
        coeffs = _get_perspective_coeffs(src_points, dst_points)
        warpped = self.image.transform((width, height), Image.PERSPECTIVE, coeffs)

        warpped.save(fp)


class Predictor:
    _q = np.array([
        [-.5, .5, .5, -.5],
        [-.5, -.5, .5, .5],
        [1., 1., 1., 1.]
    ])
    _scaling_const = 7.75

    def __init__(self, wpodnet: WPODNet):
        self.wpodnet = wpodnet
        self.wpodnet.eval()

    def _resize_to_fixed_ratio(self, image: Image.Image) -> Image.Image:
        h, w = image.height, image.width

        wh_ratio = max(h, w) / min(h, w)
        side = int(wh_ratio * 288)
        bound_dim = min(side + side % 16, 608)

        factor = bound_dim / min(h, w)
        reg_w, reg_h = int(w * factor), int(h * factor)

        return image.resize((reg_w, reg_h))

    def _to_torch_image(self, image: Image.Image) -> torch.Tensor:
        tensor = to_tensor(image)
        return tensor.unsqueeze_(0)

    def _inference(self, image: torch.Tensor) -> Tuple[np.ndarray, np.ndarray]:
        with torch.no_grad():
            probs, affines = self.wpodnet.forward(image)

        # Convert to squeezed numpy array
        # grid_w: The number of anchors in row
        # grid_h: The number of anchors in column
        probs = np.squeeze(probs.cpu().numpy())[0]     # (grid_h, grid_w)
        affines = np.squeeze(affines.cpu().numpy())  # (6, grid_h, grid_w)

        return probs, affines

    def _get_max_anchor(self, probs: np.ndarray) -> Tuple[int, int]:
        return np.unravel_index(probs.argmax(), probs.shape)

    def _get_bounds(self, affines: np.ndarray, anchor_y: int, anchor_x: int) -> np.ndarray:
        # Compute theta
        theta = affines[:, anchor_y, anchor_x]
        theta = theta.reshape((2, 3))
        theta[0, 0] = max(theta[0, 0], 0.0)
        theta[1, 1] = max(theta[1, 1], 0.0)

        # Convert theta into the bounding polygon
        bounds = np.matmul(theta, self._q) * self._scaling_const

        # Normalize the bounds
        _, grid_h, grid_w = affines.shape
        bounds[0] = (bounds[0] + anchor_x + .5) / grid_w
        bounds[1] = (bounds[1] + anchor_y + .5) / grid_h

        return np.transpose(bounds)

    def predict(self, image: Image.Image) -> Prediction:
        orig_h, orig_w = image.height, image.width

        # Resize the image to fixed ratio
        # This operation is convienence for setup the anchors
        resized = self._resize_to_fixed_ratio(image)
        resized = self._to_torch_image(resized)
        resized = resized.to(self.wpodnet.device)

        # Inference with WPODNet
        # probs: The probability distribution of the location of license plate
        # affines: The predicted affine matrix
        probs, affines = self._inference(resized)

        # Get the theta with maximum probability
        max_prob = np.amax(probs)
        anchor_y, anchor_x = self._get_max_anchor(probs)
        bounds = self._get_bounds(affines, anchor_y, anchor_x)

        bounds[:, 0] *= orig_w
        bounds[:, 1] *= orig_h

        return Prediction(
            image=image,
            bounds=bounds.astype(np.int32),
            confidence=max_prob
        )
