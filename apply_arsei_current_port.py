#!/usr/bin/env python3
"""Patch current FFmpeg h2645 SEI code with Annotated Regions SEI support."""

from pathlib import Path


ROOT = Path.cwd()
H_PATH = ROOT / "libavcodec" / "h2645_sei.h"
C_PATH = ROOT / "libavcodec" / "h2645_sei.c"


def read_file(path: Path) -> str:
    """Read a UTF-8 source file."""
    if not path.exists():
        raise FileNotFoundError(f"Missing expected file: {path}")
    return path.read_text(encoding="utf-8")


def write_file(path: Path, text: str) -> None:
    """Write a UTF-8 source file."""
    path.write_text(text, encoding="utf-8", newline="\n")


def insert_after(text: str, needle: str, insertion: str, label: str) -> str:
    """Insert text after the first matching needle unless already present."""
    if insertion.strip() in text:
        return text
    if needle not in text:
        raise RuntimeError(f"Could not find insertion point: {label}")
    return text.replace(needle, needle + insertion, 1)


def insert_before(text: str, needle: str, insertion: str, label: str) -> str:
    """Insert text before the first matching needle unless already present."""
    if insertion.strip() in text:
        return text
    if needle not in text:
        raise RuntimeError(f"Could not find insertion point: {label}")
    return text.replace(needle, insertion + needle, 1)


def patch_header(text: str) -> str:
    """Patch libavcodec/h2645_sei.h."""
    text = insert_after(
        text,
        '#include "sei.h"\n',
        """
#define ANNOTATED_REGIONS_SEI 1
#define ANNOTATED_REGIONS_LABEL_MAX_SIZE 256
#define ANNOTATED_REGIONS_MAX_NUM_OBJS 256

""",
        "h2645_sei.h includes",
    )

    text = insert_before(
        text,
        "typedef struct H2645SEI {\n",
        """#if ANNOTATED_REGIONS_SEI
typedef struct H2645SEIAnnotatedRegionLabel {
    int label_idx;
    int label_valid;
    char label[ANNOTATED_REGIONS_LABEL_MAX_SIZE];
} H2645SEIAnnotatedRegionLabel;

typedef struct H2645SEIAnnotatedRegionObject {
    int object_idx;
    int object_valid;
    int label_idx;
    int bounding_box_valid;
    int bounding_box_top;
    int bounding_box_left;
    int bounding_box_width;
    int bounding_box_height;
    int partial_obj_flag;
    int obj_confidence;
} H2645SEIAnnotatedRegionObject;

typedef struct H2645SEIAnnotatedRegions {
    int present;
    int annotated_reg_cancel_flag;
    int not_optimized_for_viewing_flag;
    int true_motion_flag;
    int occluded_obj_flag;
    int partial_obj_flag_present_flag;
    int obj_label_present_flag;
    int obj_conf_info_present_flag;
    int obj_conf_length;
    int obj_label_lang_present_flag;
    int num_label_updates;
    int num_object_updates;
    int num_bbox;
    char obj_label_lang[ANNOTATED_REGIONS_LABEL_MAX_SIZE];
    H2645SEIAnnotatedRegionLabel label[ANNOTATED_REGIONS_MAX_NUM_OBJS];
    H2645SEIAnnotatedRegionObject object[ANNOTATED_REGIONS_MAX_NUM_OBJS];
} H2645SEIAnnotatedRegions;
#endif

""",
        "H2645SEI annotated region structs",
    )

    text = insert_after(
        text,
        "    H2645SEIContentLight content_light;\n",
        """
#if ANNOTATED_REGIONS_SEI
    H2645SEIAnnotatedRegions annotated_regions;
#endif
""",
        "H2645SEI.annotated_regions field",
    )

    return text


def patch_source(text: str) -> str:
    """Patch libavcodec/h2645_sei.c."""
    text = insert_after(
        text,
        '#include "libavutil/buffer.h"\n',
        '#include "libavutil/detection_bbox.h"\n',
        "detection_bbox include",
    )

    text = insert_before(
        text,
        "static int decode_film_grain_characteristics(H2645SEIFilmGrainCharacteristics *h,\n",
        """#if ANNOTATED_REGIONS_SEI
static void initialize_annotated_regions(H2645SEIAnnotatedRegions *h)
{
    memset(h, 0, sizeof(*h));

    for (int i = 0; i < ANNOTATED_REGIONS_MAX_NUM_OBJS; i++) {
        h->object[i].object_idx = -1;
        h->object[i].label_idx = -1;
        h->label[i].label_idx = -1;
    }
}

static int read_annotated_regions_string(GetBitContext *gb, char *dst, size_t dst_size)
{
    int index = 0;

    if (!dst || !dst_size)
        return AVERROR(EINVAL);

    while ((get_bits_count(gb) % 8) != 0)
        skip_bits1(gb);

    for (;;) {
        int data;

        if (get_bits_left(gb) < 8)
            return AVERROR_INVALIDDATA;

        data = get_bits(gb, 8);

        if (index + 1 < dst_size)
            dst[index++] = (char)data;

        if (data == '\\0')
            break;
    }

    dst[dst_size - 1] = '\\0';
    return 0;
}

static int decode_annotated_regions(H2645SEIAnnotatedRegions *h, GetBitContext *gb)
{
    h->annotated_reg_cancel_flag = get_bits1(gb);
    h->present = !h->annotated_reg_cancel_flag;

    if (!h->present) {
        initialize_annotated_regions(h);
        h->annotated_reg_cancel_flag = 1;
        return 0;
    }

    h->not_optimized_for_viewing_flag = get_bits1(gb);
    h->true_motion_flag = get_bits1(gb);
    h->occluded_obj_flag = get_bits1(gb);
    h->partial_obj_flag_present_flag = get_bits1(gb);
    h->obj_label_present_flag = get_bits1(gb);
    h->obj_conf_info_present_flag = get_bits1(gb);

    if (h->obj_conf_info_present_flag)
        h->obj_conf_length = get_bits(gb, 4) + 1;

    if (h->obj_label_present_flag) {
        h->obj_label_lang_present_flag = get_bits1(gb);

        if (h->obj_label_lang_present_flag) {
            int ret = read_annotated_regions_string(gb, h->obj_label_lang, sizeof(h->obj_label_lang));
            if (ret < 0)
                return ret;
        }

        h->num_label_updates = get_ue_golomb_long(gb);

        for (int count = 0; count < h->num_label_updates; count++) {
            int label_idx = get_ue_golomb_long(gb);

            if (label_idx < 0 || label_idx >= ANNOTATED_REGIONS_MAX_NUM_OBJS)
                return AVERROR_INVALIDDATA;

            h->label[label_idx].label_idx = label_idx;
            h->label[label_idx].label_valid = !get_bits1(gb);

            if (h->label[label_idx].label_valid) {
                int ret = read_annotated_regions_string(gb, h->label[label_idx].label, sizeof(h->label[label_idx].label));
                if (ret < 0)
                    return ret;
            } else {
                h->label[label_idx].label_idx = -1;
                h->label[label_idx].label_valid = 0;
                h->label[label_idx].label[0] = '\\0';
            }
        }
    }

    h->num_object_updates = get_ue_golomb_long(gb);

    for (int count = 0; count < h->num_object_updates; count++) {
        int object_idx = get_ue_golomb_long(gb);

        if (object_idx < 0 || object_idx >= ANNOTATED_REGIONS_MAX_NUM_OBJS)
            return AVERROR_INVALIDDATA;

        h->object[object_idx].object_idx = object_idx;
        h->object[object_idx].object_valid = !get_bits1(gb);

        if (h->object[object_idx].object_valid) {
            int bb_update_flag;

            if (h->obj_label_present_flag) {
                int label_update_flag = get_bits1(gb);

                if (label_update_flag) {
                    int label_idx = get_ue_golomb_long(gb);

                    if (label_idx < 0 || label_idx >= ANNOTATED_REGIONS_MAX_NUM_OBJS)
                        return AVERROR_INVALIDDATA;

                    h->object[object_idx].label_idx = label_idx;
                }
            }

            bb_update_flag = get_bits1(gb);

            if (bb_update_flag) {
                h->object[object_idx].bounding_box_valid = !get_bits1(gb);

                if (h->object[object_idx].bounding_box_valid) {
                    h->object[object_idx].bounding_box_top = get_bits(gb, 16);
                    h->object[object_idx].bounding_box_left = get_bits(gb, 16);
                    h->object[object_idx].bounding_box_width = get_bits(gb, 16);
                    h->object[object_idx].bounding_box_height = get_bits(gb, 16);

                    if (h->partial_obj_flag_present_flag)
                        h->object[object_idx].partial_obj_flag = get_bits1(gb);

                    if (h->obj_conf_info_present_flag)
                        h->object[object_idx].obj_confidence = get_bits(gb, h->obj_conf_length);
                } else {
                    h->object[object_idx].bounding_box_top = -1;
                    h->object[object_idx].bounding_box_left = -1;
                    h->object[object_idx].bounding_box_width = -1;
                    h->object[object_idx].bounding_box_height = -1;
                }
            }
        } else {
            h->object[object_idx].object_idx = -1;
            h->object[object_idx].object_valid = 0;
            h->object[object_idx].label_idx = -1;
            h->object[object_idx].bounding_box_valid = 0;
        }
    }

    return 0;
}

static int add_annotated_regions_side_data(AVFrame *frame, H2645SEIAnnotatedRegions *ar)
{
    AVDetectionBBoxHeader *header;
    int count = 0;
    int out_index = 0;

    if (!ar->present)
        return 0;

    for (int i = 0; i < ANNOTATED_REGIONS_MAX_NUM_OBJS; i++) {
        if (ar->object[i].object_valid && ar->object[i].bounding_box_valid)
            count++;
    }

    if (!count)
        return 0;

    header = av_detection_bbox_create_side_data(frame, count);
    if (!header)
        return AVERROR(ENOMEM);

    snprintf(header->source, sizeof(header->source), "H.264/5 Annotated Regions SEI");

    for (int i = 0; i < ANNOTATED_REGIONS_MAX_NUM_OBJS; i++) {
        H2645SEIAnnotatedRegionObject *obj = &ar->object[i];
        AVDetectionBBox *bbox;
        int label_idx;

        if (!obj->object_valid || !obj->bounding_box_valid)
            continue;

        bbox = av_get_detection_bbox(header, out_index++);
        bbox->x = obj->bounding_box_left;
        bbox->y = obj->bounding_box_top;
        bbox->w = obj->bounding_box_width;
        bbox->h = obj->bounding_box_height;

        label_idx = obj->label_idx;
        if (ar->obj_label_present_flag &&
            label_idx >= 0 &&
            label_idx < ANNOTATED_REGIONS_MAX_NUM_OBJS &&
            ar->label[label_idx].label_valid &&
            ar->label[label_idx].label[0]) {
            snprintf(bbox->classify_labels[0], sizeof(bbox->classify_labels[0]), "%s", ar->label[label_idx].label);
            bbox->classify_count = 1;
        }
    }

    return 0;
}
#endif

""",
        "annotated regions decoder helpers",
    )

    text = insert_after(
        text,
        "    case SEI_TYPE_CONTENT_LIGHT_LEVEL_INFO:\n        return decode_nal_sei_content_light_info(&h->content_light, gbyte);\n",
        """#if ANNOTATED_REGIONS_SEI
    case SEI_TYPE_ANNOTATED_REGIONS:
        return decode_annotated_regions(&h->annotated_regions, gb);
#endif
""",
        "SEI_TYPE_ANNOTATED_REGIONS dispatch",
    )

    text = insert_after(
        text,
        "    dst->content_light         = src->content_light;\n",
        """
#if ANNOTATED_REGIONS_SEI
    dst->annotated_regions    = src->annotated_regions;
#endif
""",
        "annotated regions ctx copy",
    )

    text = insert_after(
        text,
        "    ret = h2645_sei_to_side_data(avctx, sei, &frame->side_data, &frame->nb_side_data);\n    if (ret < 0)\n        return ret;\n",
        """
#if ANNOTATED_REGIONS_SEI
    ret = add_annotated_regions_side_data(frame, &sei->annotated_regions);
    if (ret < 0)
        return ret;
#endif
""",
        "annotated regions frame side data",
    )

    text = insert_after(
        text,
        "    s->content_light.present = 0;\n",
        """
#if ANNOTATED_REGIONS_SEI
    initialize_annotated_regions(&s->annotated_regions);
#endif
""",
        "annotated regions reset",
    )

    return text


def main() -> None:
    """Apply all source edits."""
    header = read_file(H_PATH)
    source = read_file(C_PATH)

    patched_header = patch_header(header)
    patched_source = patch_source(source)

    write_file(H_PATH, patched_header)
    write_file(C_PATH, patched_source)

    print(f"patched: {H_PATH}")
    print(f"patched: {C_PATH}")


if __name__ == "__main__":
    main()
