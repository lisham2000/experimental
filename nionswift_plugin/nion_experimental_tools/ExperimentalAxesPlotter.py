"""Standalone metadata-driven experimental Axis Plotter panel.

Intended long-term design:

    - UI / plotting code consumes an AxesStream only.
    - AxesStream accessor functions are the only way UI / plotting code reads axes.
    - Raw metadata navigation is currently temporary and isolated in one section.
    - The temporary stream construction section can later be replaced by a real
      stream object supplied directly by metadata / niondata.

    - Select/focus a display panel.
    - The panel automatically loads axes from that display panel's data item.
    - Existing overlays on other data items remain visible.
    - Refresh remains as a manual fallback.
"""

from __future__ import annotations

import types
import typing

from dataclasses import dataclass

from nion.typeshed import API_1_0 as Facade
from nion.utils import Geometry


T = typing.TypeVar("T")


def temporary(reason: str) -> typing.Callable[[T], T]:
    """Mark an object as temporary without changing runtime behaviour."""

    def decorator(obj: T) -> T:
        setattr(obj, "__temporary__", True)
        setattr(obj, "__temporary_reason__", reason)
        return obj

    return decorator


PANEL_ID = "experimental-axis-plotter"
PANEL_TITLE = "[Experimental] Axis plotter"

AXIS_TRANSFORMATION_MATRICES_METADATA_PATH = "instrument.axis_transformation_matrices"

TEMPORARY_STREAM_REASON = (
    "Temporary stream construction. Raw metadata navigation and parsing lives "
    "here until a real AxesStream exists directly in metadata/niondata."
)


# --------------------------------------------------------------------------------------
# Shared data types
# --------------------------------------------------------------------------------------

class ApiGraphicLike(typing.Protocol):
    """Minimal Swift API graphic/region interface used by overlay drawing."""

    def set_property(self, key: str, value: object) -> None:
        ...


class ApiXDataLike(typing.Protocol):
    """Minimal xdata interface used to obtain the active data shape."""

    @property
    def data_shape(self) -> typing.Sequence[int]:
        ...


class ApiDataItemLike(typing.Protocol):
    """Minimal Swift API data-item interface used by the axis plotter.

    uuid:
        UUID-like Swift facade value used as the overlay dictionary key. T
    metadata:
        Metadata mapping used only by the temporary stream-construction layer.
    xdata:
        XData facade exposing data_shape for display-vector normalisation.
    """

    @property
    def uuid(self) -> object:
        ...

    @property
    def metadata(self) -> typing.Mapping[object, object]:
        ...

    @property
    def xdata(self) -> ApiXDataLike:
        ...

    def add_line_region(self, start_y: float, start_x: float, end_y: float, end_x: float) -> ApiGraphicLike:
        ...

    def remove_region(self, graphic: ApiGraphicLike) -> None:
        ...


class ApiDisplayLike(typing.Protocol):
    """Minimal Swift API display interface used to find a data item."""

    @property
    def data_item(self) -> ApiDataItemLike | None:
        ...


class ApiDocumentWindowLike(typing.Protocol):
    """Minimal Swift API document-window interface used for active display lookup."""

    @property
    def target_display(self) -> ApiDisplayLike | None:
        ...

    @property
    def target_data_item(self) -> ApiDataItemLike | None:
        ...


class ApiApplicationLike(typing.Protocol):
    """Minimal root application interface exposing document windows."""

    @property
    def document_windows(self) -> typing.Sequence[ApiDocumentWindowLike]:
        ...


class ApiLike(typing.Protocol):
    """Minimal root Swift API interface used by this panel."""

    @property
    def application(self) -> ApiApplicationLike:
        ...


class ApiBrokerLike(typing.Protocol):
    """Minimal API broker protocol used by the extension entry point."""

    def get_api(self, version: str) -> Facade.API:
        ...


class EventListenerLike(typing.Protocol):
    """Minimal event-listener interface returned by Nion event listen calls."""

    def close(self) -> None:
        ...


StoredGraphic = tuple[ApiDataItemLike, ApiGraphicLike]
AxisGraphicKey = tuple[str, str]


@dataclass(frozen=True)
class CoordinateAxis:
    """Parsed coordinate-axis data used by the UI and overlay renderer."""

    axis_id: str
    display_name: str
    axis_type: tuple[str, str]
    origin: Geometry.FloatPoint
    x_vector: Geometry.FloatPoint
    y_vector: Geometry.FloatPoint
    color: str
    metadata_source: str


@dataclass(frozen=True)
class VisibleAxisOverlay:
    """Currently visible axis overlay graphics on one API data item."""
    data_item_key: str
    axis_id: str
    axis: CoordinateAxis
    graphics_data_item: ApiDataItemLike
    color: str
    graphics: tuple[StoredGraphic, ...]


# --------------------------------------------------------------------------------------
# AxesStream accessor functions
#
# UI and plotting code should only use these functions to read axes stream data.
# They define the stable stream-accessor boundary
# expected to remain when the real stream object moves into metadata / niondata.
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AxesStream:
    """Axes stream object consumed by UI / plotting code.

    source_data_item:
        Data item used to construct the stream.
    axes:
        Immutable mapping from axis id to parsed CoordinateAxis.
    metadata_source:
        Metadata source path used to construct the stream.
    """

    source_data_item: ApiDataItemLike
    axes: typing.Mapping[str, CoordinateAxis]
    metadata_source: str


def get_source_data_item_from_axes_stream(axes_stream: AxesStream) -> ApiDataItemLike:
    """Return the source data item used to build this axes stream."""

    return axes_stream.source_data_item


def get_axes_from_axes_stream(axes_stream: AxesStream) -> typing.Mapping[str, CoordinateAxis]:
    """Return all axes from an axes stream."""

    return axes_stream.axes


def get_axis_from_axes_stream(axes_stream: AxesStream, axis_id: str) -> CoordinateAxis | None:
    """Return a single axis from an axes stream."""

    return axes_stream.axes.get(axis_id)


def get_metadata_source_from_axes_stream(axes_stream: AxesStream) -> str:
    """Return the metadata source used to construct this axes stream."""

    return axes_stream.metadata_source


# --------------------------------------------------------------------------------------
# Temporary stream construction - metadata navigation lives here for now
#
# This block should eventually be replaced by a real AxesStream supplied by
# metadata / niondata. Until then a temporary method for raw metadata navigation
# and parsing is intentionally collected here.
# --------------------------------------------------------------------------------------

@temporary(TEMPORARY_STREAM_REASON)
def _is_mapping(value: typing.Any) -> typing.TypeGuard[typing.Mapping[typing.Any, typing.Any]]:
    return isinstance(value, typing.Mapping)


@temporary(TEMPORARY_STREAM_REASON)
def _metadata_get_path(metadata: typing.Mapping[typing.Any, typing.Any], path: str) -> typing.Any:
    """Read a dotted metadata path from the known metadata dictionary shape."""

    if path in metadata:
        return metadata[path]

    current: typing.Any = metadata

    for part in path.split("."):
        if not _is_mapping(current):
            return None

        if part not in current:
            return None

        current = current[part]

    return current


@temporary(TEMPORARY_STREAM_REASON)
def get_data_item_metadata(data_item: ApiDataItemLike) -> typing.Mapping[typing.Any, typing.Any]:
    """Return metadata from the known data-item shape."""

    metadata = data_item.metadata

    if _is_mapping(metadata):
        return metadata

    return {}


@temporary(TEMPORARY_STREAM_REASON)
def get_axis_transformation_matrices_metadata(data_item: ApiDataItemLike) -> typing.Mapping[typing.Any, typing.Any] | None:
    """Return instrument axis transformation matrices from metadata."""

    axis_transformation_matrices = _metadata_get_path(
        get_data_item_metadata(data_item),
        AXIS_TRANSFORMATION_MATRICES_METADATA_PATH
    )

    if _is_mapping(axis_transformation_matrices):
        return axis_transformation_matrices

    return None


@temporary(TEMPORARY_STREAM_REASON)
def _read_float_point(value: typing.Any) -> Geometry.FloatPoint | None:
    """Read a point/vector from sequence, y/x mapping, or 0/1 mapping."""

    if isinstance(value, typing.Sequence) and not isinstance(value, str) and len(value) >= 2:
        try:
            return Geometry.FloatPoint(y=float(value[0]), x=float(value[1]))
        except (TypeError, ValueError):
            return None

    if _is_mapping(value):
        for y_key, x_key in (("y", "x"), (0, 1), ("0", "1")):
            if y_key in value and x_key in value:
                try:
                    return Geometry.FloatPoint(
                        y=float(value[y_key]),
                        x=float(value[x_key])
                    )
                except (TypeError, ValueError):
                    return None

    return None


@temporary(TEMPORARY_STREAM_REASON)
def _humanize_axis_id(axis_id: str) -> str:
    names = {
        "tv": "TV",
        "scan": "Scan",
        "gun": "Gun",
        "mc": "MC",
        "eels": "EELS",
        "correctoraxis": "Corrector Axis",
        "postsample": "Post Sample",
        "stageaxis": "Stage Axis",
        "stagetiltaxis": "Stage Tilt Axis"
    }

    normalized = axis_id.lower()

    if normalized in names:
        return names[normalized]

    return axis_id.replace("_", " ").replace("-", " ").title()


@temporary(TEMPORARY_STREAM_REASON)
def _default_axis_color(axis_id: str) -> str:
    normalized = "".join(ch.lower() for ch in axis_id if ch.isalnum())

    if normalized == "tv":
        return "#77FF1C"

    if normalized in {"correctoraxis", "corrector"}:
        return "#FC3A0F"

    if normalized in {"eels", "eelsaxis"}:
        return "#FF7B00"

    if normalized == "mc":
        return "#E64DFF"

    if normalized in {"postsample", "postsampleaxis", "post"}:
        return "#CCB1B1"

    if normalized == "scan":
        return "#FF0090"

    if normalized in {"stageaxis", "stage"}:
        return "#00F2FF"

    if normalized in {"stagetiltaxis", "stagetilt"}:
        return "#EEFF00"

    if normalized == "gun":
        return "#4DA3FF"

    return "#8E8E93"


@temporary(TEMPORARY_STREAM_REASON)
def _as_float(value: typing.Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@temporary(TEMPORARY_STREAM_REASON)
def _metadata_key_sort_value(key: typing.Any) -> tuple[int, int | str]:
    """Return a stable sort key for scalar metadata entries.

    Numeric-looking keys are sorted numerically so keys such as 0, 1, 2, 10
    are ordered correctly. Non-numeric keys fall back to string ordering.
    """

    if isinstance(key, int) and not isinstance(key, bool):
        return 0, key

    key_text = str(key)

    try:
        return 0, int(key_text)
    except ValueError:
        return 1, key_text


@temporary(TEMPORARY_STREAM_REASON)
def _read_matrix_from_mapping(value: typing.Mapping[typing.Any, typing.Any]) -> tuple[tuple[str, str], Geometry.FloatPoint, Geometry.FloatPoint] | None:
    """Read named basis vectors from one axis transformation matrix."""

    explicit_pairs: tuple[tuple[typing.Any, typing.Any], ...] = (
        ("x_vector", "y_vector"),
        ("x", "y"),
        ("X", "Y"),
        ("a", "b"),
        ("A", "B"),
        ("0", "1"),
        (0, 1)
    )

    for first_key, second_key in explicit_pairs:
        if first_key in value and second_key in value:
            first_vector = _read_float_point(value[first_key])
            second_vector = _read_float_point(value[second_key])

            if first_vector is not None and second_vector is not None:
                first_name = str(first_key)
                second_name = str(second_key)

                if first_name == "x_vector":
                    first_name = "x"

                if second_name == "y_vector":
                    second_name = "y"

                return (first_name, second_name), first_vector, second_vector

    vector_items: list[tuple[str, typing.Any]] = []

    for key, item_value in value.items():
        if isinstance(item_value, typing.Sequence) and not isinstance(item_value, str):
            vector_items.append((str(key), item_value))
        elif _is_mapping(item_value):
            vector_items.append((str(key), item_value))

    vectors: list[tuple[str, Geometry.FloatPoint]] = []

    for key, item_value in vector_items:
        point = _read_float_point(item_value)

        if point is not None:
            vectors.append((key, point))

    if len(vectors) >= 2:
        first_name, first_vector = vectors[0]
        second_name, second_vector = vectors[1]
        return (first_name, second_name), first_vector, second_vector

    scalar_values: list[float] = []

    for _key, item_value in sorted(value.items(), key=lambda item: _metadata_key_sort_value(item[0])):
        scalar = _as_float(item_value)

        if scalar is not None:
            scalar_values.append(scalar)

    if len(scalar_values) >= 4:
        return (
            ("x", "y"),
            Geometry.FloatPoint(y=scalar_values[0], x=scalar_values[1]),
            Geometry.FloatPoint(y=scalar_values[2], x=scalar_values[3])
        )

    return None


@temporary(TEMPORARY_STREAM_REASON)
def _read_matrix_axis_metadata(axis_id: str, value: typing.Any) -> CoordinateAxis | None:
    """Read one axis from instrument.axis_transformation_matrices metadata."""

    axis_type: tuple[str, str]
    x_vector: Geometry.FloatPoint
    y_vector: Geometry.FloatPoint

    if _is_mapping(value):
        read_result = _read_matrix_from_mapping(value)

        if read_result is None:
            return None

        axis_type, x_vector, y_vector = read_result

    elif isinstance(value, typing.Sequence) and not isinstance(value, str):
        first_vector: Geometry.FloatPoint | None = None
        second_vector: Geometry.FloatPoint | None = None

        if len(value) >= 2:
            first_vector = _read_float_point(value[0])
            second_vector = _read_float_point(value[1])

        if first_vector is not None and second_vector is not None:
            axis_type = ("x", "y")
            x_vector = first_vector
            y_vector = second_vector

        elif len(value) >= 4:
            scalar_values = [_as_float(item) for item in value[:4]]

            if not all(item is not None for item in scalar_values):
                return None

            axis_type = ("x", "y")
            x_vector = Geometry.FloatPoint(
                y=typing.cast(float, scalar_values[0]),
                x=typing.cast(float, scalar_values[1])
            )
            y_vector = Geometry.FloatPoint(
                y=typing.cast(float, scalar_values[2]),
                x=typing.cast(float, scalar_values[3])
            )

        else:
            return None

    else:
        return None

    return CoordinateAxis(
        axis_id=axis_id,
        display_name=_humanize_axis_id(axis_id),
        axis_type=axis_type,
        origin=Geometry.FloatPoint(y=0.5, x=0.5),
        x_vector=x_vector,
        y_vector=y_vector,
        color=_default_axis_color(axis_id),
        metadata_source=AXIS_TRANSFORMATION_MATRICES_METADATA_PATH
    )


@temporary(TEMPORARY_STREAM_REASON)
def get_axes_from_axis_transformation_matrices(data_item: ApiDataItemLike) -> typing.Mapping[str, CoordinateAxis]:
    """Return axes from instrument.axis_transformation_matrices metadata."""

    axis_transformation_matrices = get_axis_transformation_matrices_metadata(data_item)

    if axis_transformation_matrices is None:
        return types.MappingProxyType({})

    axes: dict[str, CoordinateAxis] = {}

    for raw_axis_id, axis_metadata in axis_transformation_matrices.items():
        axis_id = str(raw_axis_id)
        axis = _read_matrix_axis_metadata(axis_id, axis_metadata)

        if axis is not None:
            axes[axis.axis_id] = axis

    return types.MappingProxyType(axes)


@temporary(TEMPORARY_STREAM_REASON)
def get_coordinate_system_axes(data_item: ApiDataItemLike) -> typing.Mapping[str, CoordinateAxis]:
    """Return available coordinate axes from the current metadata format."""

    return get_axes_from_axis_transformation_matrices(data_item)


@temporary(TEMPORARY_STREAM_REASON)
def get_coordinate_metadata_source_summary(data_item: ApiDataItemLike) -> str:
    axis_transformation_matrices = get_axis_transformation_matrices_metadata(data_item)

    if axis_transformation_matrices is not None:
        axes = get_axes_from_axis_transformation_matrices(data_item)

        if axes:
            return AXIS_TRANSFORMATION_MATRICES_METADATA_PATH

    return "none"


@temporary(TEMPORARY_STREAM_REASON)
def create_axes_stream_from_data_item(data_item: ApiDataItemLike) -> AxesStream | None:
    """Construct an AxesStream from the known data-item metadata shape."""

    axes = get_coordinate_system_axes(data_item)

    if not axes:
        return None

    return AxesStream(
        source_data_item=data_item,
        axes=types.MappingProxyType(dict(axes)),
        metadata_source=get_coordinate_metadata_source_summary(data_item)
    )


@temporary(TEMPORARY_STREAM_REASON)
def create_axes_stream_from_display_item(display_item: ApiDisplayLike) -> AxesStream | None:
    """Construct an AxesStream from the known display-item shape."""

    data_item = display_item.data_item

    if data_item is None:
        return None

    return create_axes_stream_from_data_item(data_item)
