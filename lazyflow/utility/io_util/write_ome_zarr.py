import dataclasses
import logging
from collections import OrderedDict as ODict
from functools import partial
from typing import List, Tuple, Dict, OrderedDict

import numpy
import zarr
from zarr.storage import FSStore, contains_array

from ilastik import __version__ as ilastik_version
from lazyflow.operators import OpReorderAxes
from lazyflow.roi import determineBlockShape, roiFromShape, roiToSlice
from lazyflow.slot import Slot
from lazyflow.utility import OrderedSignal, PathComponents, BigRequestStreamer
from lazyflow.utility.io_util import multiscaleStore
from lazyflow.utility.io_util.OMEZarrStore import OME_ZARR_V_0_4_KWARGS

logger = logging.getLogger(__name__)

Shape = Tuple[int, ...]
TaggedShape = OrderedDict[str, int]  # { axis: size }
OrderedScaling = OrderedDict[str, float]  # { axis: scaling }
ScalingsByScaleKey = OrderedDict[str, OrderedScaling]  # { scale_key: { axis: scaling } }

SPATIAL_AXES = ["z", "y", "x"]


@dataclasses.dataclass
class ImageMetadata:
    path: str
    scale: OrderedScaling
    translation: Dict[str, float]


def _get_chunk_shape(tagged_image_shape: TaggedShape, dtype) -> Shape:
    """Determine chunk shape for OME-Zarr storage. 1 for t and c,
    ilastik default rules for zyx, with a target of 512KB per chunk."""
    if isinstance(dtype, numpy.dtype):  # Extract raw type class
        dtype = dtype.type
    dtype_bytes = dtype().nbytes
    tagged_maxshape = tagged_image_shape.copy()
    tagged_maxshape["t"] = 1
    tagged_maxshape["c"] = 1
    chunk_shape = determineBlockShape(list(tagged_maxshape.values()), 512_000.0 / dtype_bytes)  # 512KB chunk size
    return chunk_shape


def _compute_new_scaling_factors(
    original_tagged_shape: TaggedShape, chunk_shape: Shape, compute_downscales: bool
) -> List[OrderedScaling]:
    """
    Computes scaling "factors".
    Technically they are divisors for the shape (factor 2.0 means half the shape).
    Downscaling is done by a factor of 2 in all spatial dimensions until:
    - the dataset would be less than 4 x chunk size (2MiB)
    - an axis that started non-singleton would become singleton
    Returns list of scaling factor dicts by axis, starting with original scale.
    The scaling level that meets one of the exit conditions is excluded.
    Raises if more than 20 scales are computed (sanity).
    """
    assert len(chunk_shape) == len(original_tagged_shape), "Chunk shape and tagged shape must have same length"
    original_scale = ODict([(a, 1.0) for a in original_tagged_shape.keys()])
    scalings = [original_scale]
    if not compute_downscales:
        return scalings
    sanity_limit = 20
    for i in range(sanity_limit):
        if i == sanity_limit:
            raise ValueError(f"Too many scales computed, limit={sanity_limit}. Please report this to the developers.")
        new_scaling = ODict(
            [
                (a, 2.0 ** (i + 1)) if a in SPATIAL_AXES and original_tagged_shape[a] > 1 else (a, 1.0)
                for a in original_tagged_shape.keys()
            ]
        )
        new_shape = _scale_tagged_shape(original_tagged_shape, new_scaling)
        if _is_less_than_4_chunks(new_shape, chunk_shape) or _reduces_any_axis_to_singleton(
            new_shape.values(), original_tagged_shape.values()
        ):
            break
        raise NotImplementedError("See _apply_scaling_method()")  # scalings.append(new_scaling)
    return scalings


def _reduces_any_axis_to_singleton(new_shape: Shape, original_shape: Shape):
    return any(new <= 1 < orig for new, orig in zip(new_shape, original_shape))


def _is_less_than_4_chunks(new_shape: TaggedShape, chunk_shape: Shape):
    spatial_shape = [s for a, s in new_shape.items() if a in SPATIAL_AXES]
    return numpy.prod(spatial_shape) < 4 * numpy.prod(chunk_shape)


def _scale_tagged_shape(original_tagged_shape: TaggedShape, scaling: OrderedScaling) -> TaggedShape:
    assert all(s > 0 for s in scaling.values()), f"Invalid scaling: {scaling}"
    return ODict(
        [
            (a, _round_like_scaling_method(s / scaling[a]) if a in scaling else s)
            for a, s in original_tagged_shape.items()
        ]
    )


def _round_like_scaling_method(value: float) -> int:
    """For calculating scaled shape after applying the scaling method.
    Different scaling methods round differently, so we need to match that.
    E.g. scaling by stepwise downsampling like image[::2, ::2] always rounds up,
    while e.g. skimage.transform.rescale rounds mathematically like standard round()."""
    return int(value)


def _get_input_multiscales_matching_export(
    input_scales: multiscaleStore.Multiscales, export_shape: TaggedShape, compute_downscales: bool
) -> multiscaleStore.Multiscales:
    """Filter for multiscales entry that matches source image, plus lower scales if compute_downscales is True."""
    matching_scales = []
    # Multiscales is ordered from highest to lowest resolution, so start collecting once match found
    match_found = False
    for key, scale_shape in input_scales.items():
        if all(scale_shape[a] == export_shape[a] for a in scale_shape.keys()):
            match_found = True
            matching_scales.append((key, scale_shape))
            if not compute_downscales:
                break
        elif match_found:
            matching_scales.append((key, scale_shape))
    assert len(matching_scales) > 0, "Should be impossible, input must be one of the scales"
    return ODict(matching_scales)


def _multiscales_to_scaling_factors(
    multiscales: multiscaleStore.Multiscales, base_shape: TaggedShape
) -> List[OrderedScaling]:
    scalings = []
    for scale_shape in multiscales.values():
        # base_shape / scale_shape: See note on scaling divisors in _get_scalings
        tagged_factors = ODict(
            [(a, base / s) for a, s, base in zip(scale_shape.keys(), scale_shape.values(), base_shape.values())]
        )
        scalings.append(tagged_factors)
    return scalings


def _match_or_create_scalings(
    input_scales: multiscaleStore.Multiscales, export_shape: TaggedShape, chunk_shape, compute_downscales: bool
) -> ScalingsByScaleKey:
    if input_scales:
        # Source image is already multiscale, match its scales
        filtered_input_scales = _get_input_multiscales_matching_export(input_scales, export_shape, compute_downscales)
        factors = _multiscales_to_scaling_factors(filtered_input_scales, export_shape)
        output_scalings = ODict(zip(filtered_input_scales.keys(), factors))
    else:
        # Compute new scale levels
        factors = _compute_new_scaling_factors(export_shape, chunk_shape, compute_downscales)
        output_scalings = ODict(zip([f"s{i}" for i in range(len(factors))], factors))
    return output_scalings


def _create_empty_zarrays(
    export_path: str,
    export_dtype,
    chunk_shape: Shape,
    export_shape: TaggedShape,
    output_scalings: ScalingsByScaleKey,
) -> Tuple[OrderedDict[str, zarr.Array], List[ImageMetadata]]:
    pc = PathComponents(export_path)
    external_path = pc.externalPath
    internal_path = pc.internalPath.lstrip("/") if pc.internalPath else None
    store = FSStore(external_path, mode="w", **OME_ZARR_V_0_4_KWARGS)
    zarrays = ODict()
    meta = []
    for scale_key, scaling in output_scalings.items():
        scale_path = f"{internal_path}/{scale_key}" if internal_path else scale_key
        scaled_shape = _scale_tagged_shape(export_shape, scaling).values()
        if contains_array(store, scale_path):
            logger.warning(f"Deleting existing dataset at {external_path}/{scale_path}.")
            del store[scale_path]
        zarrays[scale_key] = zarr.creation.zeros(
            scaled_shape, store=store, path=scale_path, chunks=chunk_shape, dtype=export_dtype
        )
        meta.append(ImageMetadata(scale_path, scaling, {}))

    return zarrays, meta


def _apply_scaling_method(
    data: numpy.typing.NDArray, current_block_roi: Tuple[List[int], List[int]], scaling: OrderedScaling
) -> Tuple[numpy.typing.NDArray, Tuple[List[int], List[int]]]:
    """Downscaling tbd, need to investigate whether blockwise scaling is feasible.
    May have to restructure the flow instead and potentially do export blockwise, then scaling afterwards."""
    raise NotImplementedError()


def _scale_and_write_block(scales: ScalingsByScaleKey, zarrays: OrderedDict[str, zarr.Array], roi, data):
    assert scales.keys() == zarrays.keys()
    for scale_key_, scaling_ in scales.items():
        if scaling_["x"] > 1.0 or scaling_["y"] > 1.0:
            logger.info(f"Scale {scale_key_}: Applying {scaling_=} to {roi=}")
            scaled_data, scaled_roi = _apply_scaling_method(data, roi, scaling_)
            slicing = roiToSlice(*scaled_roi)
        else:
            slicing = roiToSlice(*roi)
            scaled_data = data
        logger.info(f"Scale {scale_key_}: Writing data with shape={scaled_data.shape} to {slicing=}")
        zarrays[scale_key_][slicing] = scaled_data


def _write_ome_zarr_and_ilastik_metadata(
    export_path: str, multiscale_metadata: List[ImageMetadata], ilastik_meta: Dict
):
    pc = PathComponents(export_path)
    external_path = pc.externalPath
    multiscale_name = pc.internalPath.lstrip("/") if pc.internalPath else None
    ilastik_signature = {"name": "ilastik", "version": ilastik_version, "ome_zarr_exporter_version": 1}
    axis_types = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}
    axes = [{"name": tag.key, "type": axis_types[tag.key]} for tag in ilastik_meta["axistags"]]
    datasets = [
        {
            "path": image.path,
            "coordinateTransformations": [
                {"type": "scale", "scale": [image.scale[tag.key] for tag in ilastik_meta["axistags"]]}
            ],
        }
        for image in multiscale_metadata
    ]
    ome_zarr_multiscale_meta = {"_creator": ilastik_signature, "version": "0.4", "axes": axes, "datasets": datasets}
    if multiscale_name:
        ome_zarr_multiscale_meta["name"] = multiscale_name
    store = FSStore(external_path, mode="w", **OME_ZARR_V_0_4_KWARGS)
    root = zarr.group(store, overwrite=False)
    root.attrs["multiscales"] = [ome_zarr_multiscale_meta]
    for image in multiscale_metadata:
        za = zarr.Array(store, path=image.path)
        za.attrs["axistags"] = ilastik_meta["axistags"].toJSON()
        if ilastik_meta["display_mode"]:
            za.attrs["display_mode"] = ilastik_meta["display_mode"]
        if ilastik_meta["drange"]:
            za.attrs["drange"] = ilastik_meta["drange"]


def write_ome_zarr(
    export_path: str,
    image_source_slot: Slot,
    progress_signal: OrderedSignal,
    compute_downscales: bool = False,
):
    op_reorder = OpReorderAxes(parent=image_source_slot.operator)
    op_reorder.AxisOrder.setValue("tczyx")
    try:
        op_reorder.Input.connect(image_source_slot)
        reordered_source = op_reorder.Output
        progress_signal(25)
        export_shape = reordered_source.meta.getTaggedShape()
        export_dtype = reordered_source.meta.dtype
        input_scales = reordered_source.meta.scales if "scales" in reordered_source.meta else None

        chunk_shape = _get_chunk_shape(export_shape, export_dtype)
        output_scalings = _match_or_create_scalings(input_scales, export_shape, chunk_shape, compute_downscales)
        zarrays, ome_zarr_meta = _create_empty_zarrays(
            export_path, export_dtype, chunk_shape, export_shape, output_scalings
        )

        requester = BigRequestStreamer(reordered_source, roiFromShape(reordered_source.meta.shape))
        requester.resultSignal.subscribe(partial(_scale_and_write_block, output_scalings, zarrays))
        requester.progressSignal.subscribe(progress_signal)
        requester.execute()

        progress_signal(95)
        _write_ome_zarr_and_ilastik_metadata(
            export_path,
            ome_zarr_meta,
            {
                "axistags": reordered_source.meta.axistags,
                "display_mode": reordered_source.meta.get("display_mode"),
                "drange": reordered_source.meta.get("drange"),
            },
        )
    finally:
        op_reorder.cleanUp()
