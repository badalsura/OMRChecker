import os
from collections import defaultdict
from typing import Any

import cv2
import matplotlib.pyplot as plt
import numpy as np

from src.constants.common import (
    CLR_BLACK,
    CLR_DARK_GRAY,
    CLR_GRAY,
    GLOBAL_PAGE_THRESHOLD_BLACK,
    GLOBAL_PAGE_THRESHOLD_WHITE,
    TEXT_SIZE,
)
from src.logger import logger
from src.utils.image import CLAHE_HELPER, ImageUtils
from src.utils.interaction import InteractionUtils


class ImageInstanceOps:
    """Class to hold fine-tuned utilities for a group of images. One instance for each processing directory."""

    def __init__(self, tuning_config):
        super().__init__()
        self.tuning_config = tuning_config
        self.save_image_level = tuning_config.outputs.save_image_level
        # Per-instance so that concurrent templates/requests don't share debug images
        self.save_img_list: Any = defaultdict(list)
        # Optional learned classifier (src/ml/classifiers.py) for bubble crops
        self.bubble_classifier = None
        # While companion images are tracked: geometric steps of the current
        # preprocessor (see ImagePreprocessor.record_geometry)
        self.geometry_ops = None

    def apply_preprocessors(self, file_path, in_omr, template, companions=None):
        """
        Register the sheet. companions ({key: image of the same size}, e.g. colour
        dropout variants some zones read) are updated in place to follow the same
        geometry; a companion that cannot follow is dropped.
        """
        tuning_config = self.tuning_config
        pre_processors = template.pre_processors
        # resize to conform to template, unless registration works on the original
        # pixels (or there is nothing to run, so reading resizes straight to the page)
        if pre_processors and not pre_processors[0].needs_full_resolution:
            size = (
                tuning_config.dimensions.processing_width,
                tuning_config.dimensions.processing_height,
            )
            in_omr = ImageUtils.resize_util(in_omr, *size)
            for key in list(companions or {}):
                companions[key] = ImageUtils.resize_util(companions[key], *size)

        # run pre_processors in sequence
        for pre_processor in template.pre_processors:
            if not companions:
                in_omr = pre_processor.apply_filter(in_omr, file_path)
            else:
                self.geometry_ops = []
                try:
                    in_omr = pre_processor.apply_filter(in_omr, file_path)
                    ops = self.geometry_ops
                finally:
                    self.geometry_ops = None
                if in_omr is not None:
                    self.follow_geometry(pre_processor, ops, companions, file_path)
            if in_omr is None:
                break
        return in_omr

    @staticmethod
    def follow_geometry(pre_processor, ops, companions, file_path):
        mode = getattr(pre_processor, "geometry", "unknown")
        if mode == "none":
            return
        for key in list(companions):
            image = companions[key]
            if mode == "recorded":
                for transform in ops:
                    image = transform(image)
            else:
                # Unknown step: run it again on the companion (best effort)
                image = pre_processor.apply_filter(image, file_path)
            if image is None:
                del companions[key]
            else:
                companions[key] = image

    def read_omr_response(self, template, image, name, save_dir=None):
        result = self.read_omr_response_detailed(template, image, name, save_dir)
        return (
            result["omr_response"],
            result["final_marked"],
            result["multi_marked"],
            result["multi_roll"],
        )

    def read_omr_response_detailed(self, template, image, name, save_dir=None):
        """Read all bubble fields, returning per-field values, confidence and review flags."""
        config = self.tuning_config
        auto_align = config.alignment_params.auto_align
        img = image.copy()
        # origDim = img.shape[:2]
        img = ImageUtils.resize_util(
            img, template.page_dimensions[0], template.page_dimensions[1]
        )
        if img.max() > img.min():
            img = ImageUtils.normalize_util(img)
        # Processing copies
        transp_layer = img.copy()
        final_marked = img.copy()

        morph = img.copy()
        self.append_save_img(3, morph)

        if auto_align:
            # Note: clahe is good for morphology, bad for thresholding
            morph = CLAHE_HELPER.apply(morph)
            self.append_save_img(3, morph)
            # Remove shadows further, make columns/boxes darker (less gamma)
            morph = ImageUtils.adjust_gamma(morph, config.threshold_params.GAMMA_LOW)
            # TODO: all numbers should come from either constants or config
            _, morph = cv2.threshold(morph, 220, 220, cv2.THRESH_TRUNC)
            morph = ImageUtils.normalize_util(morph)
            self.append_save_img(3, morph)
            if config.outputs.show_image_level >= 4:
                InteractionUtils.show("morph1", morph, 0, 1, config)

        # Move them to data class if needed
        # Overlay Transparencies
        alpha = 0.65
        omr_response = {}
        multi_marked, multi_roll = 0, 0

        # TODO Make this part useful for visualizing status checks
        # blackVals=[0]
        # whiteVals=[255]

        if config.outputs.show_image_level >= 5:
            all_c_box_vals = {"int": [], "mcq": []}
            # TODO: simplify this logic
            q_nums = {"int": [], "mcq": []}

        # Find Shifts for the field_blocks --> Before calculating threshold!
        if auto_align:
            # print("Begin Alignment")
            # Open : erode then dilate
            v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 10))
            morph_v = cv2.morphologyEx(morph, cv2.MORPH_OPEN, v_kernel, iterations=3)
            _, morph_v = cv2.threshold(morph_v, 200, 200, cv2.THRESH_TRUNC)
            morph_v = 255 - ImageUtils.normalize_util(morph_v)

            if config.outputs.show_image_level >= 3:
                InteractionUtils.show("morphed_vertical", morph_v, 0, 1, config=config)

            # InteractionUtils.show("morph1",morph,0,1,config=config)
            # InteractionUtils.show("morphed_vertical",morph_v,0,1,config=config)

            self.append_save_img(3, morph_v)

            morph_thr = 60  # for Mobile images, 40 for scanned Images
            _, morph_v = cv2.threshold(morph_v, morph_thr, 255, cv2.THRESH_BINARY)
            # kernel best tuned to 5x5 now
            morph_v = cv2.erode(morph_v, np.ones((5, 5), np.uint8), iterations=2)

            self.append_save_img(3, morph_v)
            # h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 2))
            # morph_h = cv2.morphologyEx(morph, cv2.MORPH_OPEN, h_kernel, iterations=3)
            # ret, morph_h = cv2.threshold(morph_h,200,200,cv2.THRESH_TRUNC)
            # morph_h = 255 - normalize_util(morph_h)
            # InteractionUtils.show("morph_h",morph_h,0,1,config=config)
            # _, morph_h = cv2.threshold(morph_h,morph_thr,255,cv2.THRESH_BINARY)
            # morph_h = cv2.erode(morph_h,  np.ones((5,5),np.uint8), iterations = 2)
            if config.outputs.show_image_level >= 3:
                InteractionUtils.show("morph_thr_eroded", morph_v, 0, 1, config=config)

            self.append_save_img(6, morph_v)

            # template relative alignment code
            for field_block in template.field_blocks:
                s, d = field_block.origin, field_block.dimensions

                match_col, max_steps, align_stride, thk = map(
                    config.alignment_params.get,
                    [
                        "match_col",
                        "max_steps",
                        "stride",
                        "thickness",
                    ],
                )
                shift, steps = 0, 0
                while steps < max_steps:
                    left_mean = np.mean(
                        morph_v[
                            s[1] : s[1] + d[1],
                            s[0] + shift - thk : -thk + s[0] + shift + match_col,
                        ]
                    )
                    right_mean = np.mean(
                        morph_v[
                            s[1] : s[1] + d[1],
                            s[0]
                            + shift
                            - match_col
                            + d[0]
                            + thk : thk
                            + s[0]
                            + shift
                            + d[0],
                        ]
                    )

                    # For demonstration purposes-
                    # if(field_block.name == "int1"):
                    #     ret = morph_v.copy()
                    #     cv2.rectangle(ret,
                    #                   (s[0]+shift-thk,s[1]),
                    #                   (s[0]+shift+thk+d[0],s[1]+d[1]),
                    #                   CLR_WHITE,
                    #                   3)
                    #     appendSaveImg(6,ret)
                    # print(shift, left_mean, right_mean)
                    left_shift, right_shift = left_mean > 100, right_mean > 100
                    if left_shift:
                        if right_shift:
                            break
                        else:
                            shift -= align_stride
                    else:
                        if right_shift:
                            shift += align_stride
                        else:
                            break
                    steps += 1

                field_block.shift = shift
                # print("Aligned field_block: ",field_block.name,"Corrected Shift:",
                #   field_block.shift,", dimensions:", field_block.dimensions,
                #   "origin:", field_block.origin,'\n')
            # print("End Alignment")

        snap_radius = config.alignment_params.block_snap_radius
        for field_block in template.field_blocks:
            field_block.shift_y = 0
            if snap_radius:
                field_block.shift, field_block.shift_y = self.snap_field_block(
                    img, field_block, snap_radius
                )
        rectify_failed = self.rectify_field_blocks(img, template)

        final_align = None
        if config.outputs.show_image_level >= 2:
            initial_align = self.draw_template_layout(img, template, shifted=False)
            final_align = self.draw_template_layout(
                img, template, shifted=True, draw_qvals=True
            )
            # appendSaveImg(4,mean_vals)
            self.append_save_img(2, initial_align)
            self.append_save_img(2, final_align)

            if auto_align:
                final_align = np.hstack((initial_align, final_align))
        self.append_save_img(5, img)

        # Get mean bubbleValues n other stats
        all_q_vals, all_q_strip_arrs, all_q_std_vals = [], [], []
        total_q_strip_no = 0
        for field_block in template.field_blocks:
            box_w, box_h = field_block.bubble_dimensions
            q_std_vals = []
            for field_block_bubbles in field_block.traverse_bubbles:
                q_strip_vals = []
                for pt in field_block_bubbles:
                    # shifted
                    x = pt.x + field_block.shift + pt.dx
                    y = pt.y + field_block.shift_y + pt.dy
                    rect = [y, y + box_h, x, x + box_w]
                    q_strip_vals.append(
                        cv2.mean(img[rect[0] : rect[1], rect[2] : rect[3]])[0]
                        # detectCross(img, rect) ? 100 : 0
                    )
                q_std_vals.append(round(np.std(q_strip_vals), 2))
                all_q_strip_arrs.append(q_strip_vals)
                # _, _, _ = get_global_threshold(q_strip_vals, "QStrip Plot",
                #   plot_show=False, sort_in_plot=True)
                # hist = getPlotImg()
                # InteractionUtils.show("QStrip "+field_block_bubbles[0].field_label, hist, 0, 1,config=config)
                all_q_vals.extend(q_strip_vals)
                # print(total_q_strip_no, field_block_bubbles[0].field_label, q_std_vals[len(q_std_vals)-1])
                total_q_strip_no += 1
            all_q_std_vals.extend(q_std_vals)

        threshold_params = config.threshold_params
        fixed_mode = threshold_params.get("mode", "adaptive") == "fixed"
        if fixed_mode:
            # One intensity line for every sheet: no per-sheet threshold search
            fixed_threshold = float(threshold_params.fixed_threshold)
            fixed_min_fill = float(threshold_params.fixed_min_fill_ratio)
            fill_margin = max(min(fixed_min_fill, 1.0 - fixed_min_fill), 0.05)
            global_std_thresh = global_thr = fixed_threshold
        else:
            global_std_thresh, _, _ = self.get_global_threshold(
                all_q_std_vals
            )  # , "Q-wise Std-dev Plot", plot_show=True, sort_in_plot=True)
        # plt.show()
        # hist = getPlotImg()
        # InteractionUtils.show("StdHist", hist, 0, 1,config=config)

        # Note: Plotting takes Significant times here --> Change Plotting args
        # to support show_image_level
        # , "Mean Intensity Histogram",plot_show=True, sort_in_plot=True)
        if not fixed_mode:
            global_thr, _, _ = self.get_global_threshold(all_q_vals, looseness=4)

        logger.info(
            f"Thresholding: \tglobal_thr: {round(global_thr, 2)} \tglobal_std_THR: {round(global_std_thresh, 2)}\t{'(Looks like a Xeroxed OMR)' if (global_thr == 255) else ''}"
        )
        # plt.show()
        # hist = getPlotImg()
        # InteractionUtils.show("StdHist", hist, 0, 1,config=config)

        # if(config.outputs.show_image_level>=1):
        #     hist = getPlotImg()
        #     InteractionUtils.show("Hist", hist, 0, 1,config=config)
        #     appendSaveImg(4,hist)
        #     appendSaveImg(5,hist)
        #     appendSaveImg(2,hist)

        model_marked_probs = self.get_model_marked_probs(img, template)

        per_omr_threshold_avg, total_q_strip_no, total_q_box_no = 0, 0, 0
        review_params = config.review_params
        field_details = {}
        for field_block in template.field_blocks:
            block_q_strip_no = 1
            box_w, box_h = field_block.bubble_dimensions
            key = field_block.name[:3]
            for field_block_bubbles in field_block.traverse_bubbles:
                if fixed_mode:
                    per_q_strip_threshold = fixed_threshold
                    strip_low_confidence = False
                else:
                    # All Black or All White case
                    no_outliers = all_q_std_vals[total_q_strip_no] < global_std_thresh
                    per_q_strip_threshold = self.get_local_threshold(
                        all_q_strip_arrs[total_q_strip_no],
                        global_thr,
                        no_outliers,
                        f"Mean Intensity Histogram for {key}.{field_block_bubbles[0].field_label}.{block_q_strip_no}",
                        config.outputs.show_image_level >= 6,
                    )
                    strip_low_confidence = self.last_local_threshold_low_confidence
                per_omr_threshold_avg += per_q_strip_threshold

                detected_bubbles = []
                bubble_details = []
                for bubble in field_block_bubbles:
                    bubble_mean = all_q_vals[total_q_box_no]
                    model_prob = (
                        None
                        if model_marked_probs is None
                        else float(model_marked_probs[total_q_box_no])
                    )
                    total_q_box_no += 1
                    x, y, field_value = (
                        bubble.x + field_block.shift + bubble.dx,
                        bubble.y + field_block.shift_y + bubble.dy,
                        bubble.field_value,
                    )
                    if fixed_mode:
                        # Marked when enough of the interior is darker than the line
                        fill_ratio = self.get_fill_ratio(
                            img, x, y, box_w, box_h, fixed_threshold
                        )
                        bubble_is_marked = fill_ratio >= fixed_min_fill
                        bubble_confidence = float(
                            np.clip(
                                abs(fill_ratio - fixed_min_fill) / fill_margin, 0, 1
                            )
                        )
                    else:
                        bubble_is_marked = per_q_strip_threshold > bubble_mean
                        # Count only pixels clearly darker than the decision threshold
                        # so printed letters and tinted backgrounds don't count as ink
                        fill_ratio = self.get_fill_ratio(
                            img,
                            x,
                            y,
                            box_w,
                            box_h,
                            per_q_strip_threshold - review_params.confidence_margin,
                        )
                        # How far the bubble sits from the decision boundary, in [0, 1]
                        bubble_confidence = float(
                            np.clip(
                                abs(per_q_strip_threshold - bubble_mean)
                                / review_params.confidence_margin,
                                0,
                                1,
                            )
                        )
                    bubble_detail = {
                        "value": field_value,
                        "x": int(x),
                        "y": int(y),
                        "w": int(box_w),
                        "h": int(box_h),
                        "mean_intensity": round(float(bubble_mean), 2),
                        "fill_ratio": round(fill_ratio, 3),
                        "marked": bool(bubble_is_marked),
                        "confidence": round(bubble_confidence, 3),
                    }
                    if model_prob is not None:
                        # The learned classifier decides; the threshold read is a cross-check
                        model_is_marked = model_prob >= 0.5
                        bubble_detail["model_marked_prob"] = round(model_prob, 4)
                        bubble_detail["model_disagrees"] = bool(
                            model_is_marked != bubble_is_marked
                        )
                        bubble_is_marked = model_is_marked
                        bubble_detail["marked"] = bool(model_is_marked)
                        bubble_detail["confidence"] = round(
                            abs(model_prob - 0.5) * 2, 3
                        )
                    bubble_details.append(bubble_detail)
                    if bubble_is_marked:
                        detected_bubbles.append(bubble)
                        cv2.rectangle(
                            final_marked,
                            (int(x + box_w / 12), int(y + box_h / 12)),
                            (
                                int(x + box_w - box_w / 12),
                                int(y + box_h - box_h / 12),
                            ),
                            CLR_DARK_GRAY,
                            3,
                        )

                        cv2.putText(
                            final_marked,
                            str(field_value),
                            (x, y),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            TEXT_SIZE,
                            (20, 20, 10),
                            int(1 + 3.5 * TEXT_SIZE),
                        )
                    else:
                        cv2.rectangle(
                            final_marked,
                            (int(x + box_w / 10), int(y + box_h / 10)),
                            (
                                int(x + box_w - box_w / 10),
                                int(y + box_h - box_h / 10),
                            ),
                            CLR_GRAY,
                            -1,
                        )

                for bubble in detected_bubbles:
                    field_label, field_value = (
                        bubble.field_label,
                        bubble.field_value,
                    )
                    # Only send rolls multi-marked in the directory
                    multi_marked_local = field_label in omr_response
                    omr_response[field_label] = (
                        (omr_response[field_label] + field_value)
                        if multi_marked_local
                        else field_value
                    )
                    multi_marked = multi_marked or multi_marked_local

                field_label = field_block_bubbles[0].field_label
                if len(detected_bubbles) == 0:
                    omr_response[field_label] = field_block.empty_val

                field_details[field_label] = self.summarize_field(
                    field_label,
                    omr_response[field_label],
                    bubble_details,
                    len(detected_bubbles),
                    strip_low_confidence,
                    review_params,
                    ["rectify_failed"] if field_block.name in rectify_failed else None,
                )

                if config.outputs.show_image_level >= 5:
                    if key in all_c_box_vals:
                        q_nums[key].append(f"{key[:2]}_c{str(block_q_strip_no)}")
                        all_c_box_vals[key].append(all_q_strip_arrs[total_q_strip_no])

                block_q_strip_no += 1
                total_q_strip_no += 1
            # /for field_block

        per_omr_threshold_avg /= total_q_strip_no
        per_omr_threshold_avg = round(per_omr_threshold_avg, 2)
        # Translucent
        cv2.addWeighted(final_marked, alpha, transp_layer, 1 - alpha, 0, final_marked)
        # Box types
        if config.outputs.show_image_level >= 6:
            # plt.draw()
            f, axes = plt.subplots(len(all_c_box_vals), sharey=True)
            f.canvas.manager.set_window_title(name)
            ctr = 0
            type_name = {
                "int": "Integer",
                "mcq": "MCQ",
                "med": "MED",
                "rol": "Roll",
            }
            for k, boxvals in all_c_box_vals.items():
                axes[ctr].title.set_text(type_name[k] + " Type")
                axes[ctr].boxplot(boxvals)
                # thrline=axes[ctr].axhline(per_omr_threshold_avg,color='red',ls='--')
                # thrline.set_label("Average THR")
                axes[ctr].set_ylabel("Intensity")
                axes[ctr].set_xticklabels(q_nums[k])
                # axes[ctr].legend()
                ctr += 1
            # imshow will do the waiting
            plt.tight_layout(pad=0.5)
            plt.show()

        if config.outputs.show_image_level >= 3 and final_align is not None:
            final_align = ImageUtils.resize_util_h(
                final_align, int(config.dimensions.display_height)
            )
            # [final_align.shape[1],0])
            InteractionUtils.show(
                "Template Alignment Adjustment", final_align, 0, 0, config=config
            )

        if config.outputs.save_detections and save_dir is not None:
            image_path = str(save_dir.joinpath(name))
            ImageUtils.save_img(image_path, final_marked)

        self.append_save_img(2, final_marked)

        if save_dir is not None:
            for i in range(config.outputs.save_image_level):
                self.save_image_stacks(i + 1, name, save_dir)

        return {
            "omr_response": omr_response,
            "final_marked": final_marked,
            "multi_marked": multi_marked,
            "multi_roll": multi_roll,
            "field_details": field_details,
            "aligned_image": img,
            "thresholds": {
                "global": round(float(global_thr), 2),
                "global_std": round(float(global_std_thresh), 2),
                "average_local": per_omr_threshold_avg,
                **({"mode": "fixed"} if fixed_mode else {}),
            },
        }

    def rectify_field_blocks(self, img, template):
        """
        Fit blocks with rectifyOnBorder onto their printed borders (src/rectify.py).
        Returns the names of blocks where that failed (their fields get flagged).
        """
        alignment = self.tuning_config.alignment_params
        default = alignment.get("rectify_on_border", False)
        failed = set()
        for field_block in template.field_blocks:
            if field_block.rectified:
                from src.rectify import reset_offsets

                reset_offsets(field_block)
            enabled = field_block.rectify_on_border
            if not (default if enabled is None else enabled):
                continue
            from src.rectify import apply_offsets, rectify_field_block

            result = rectify_field_block(
                img, field_block, alignment.get("rectify_search_px", 20)
            )
            field_block.last_rectification = result.to_dict()
            if result.ok:
                apply_offsets(field_block, result.offsets)
            else:
                logger.info(
                    f"Block '{field_block.name}' not rectified: {result.reason}"
                )
                failed.add(field_block.name)
        return failed

    @staticmethod
    def snap_field_block(img, field_block, radius):
        """Find the (dx, dy) within radius that best fits the block's printed bubbles.

        Correlates a mask of the expected bubble outlines and interiors with the
        darkness of the page around the block. Corrects residual local offsets
        (template drift, paper curl) that a global page transform leaves behind.
        """
        box_w, box_h = field_block.bubble_dimensions
        block_w, block_h = field_block.dimensions
        x0, y0 = field_block.origin
        img_h, img_w = img.shape[:2]
        left, top = x0 - radius, y0 - radius
        right, bottom = x0 + block_w + radius, y0 + block_h + radius
        if left < 0 or top < 0 or right > img_w or bottom > img_h:
            return field_block.shift, 0
        region = 255.0 - img[top:bottom, left:right].astype(np.float32)

        mask = np.zeros((int(block_h), int(block_w)), dtype=np.float32)
        for field_block_bubbles in field_block.traverse_bubbles:
            for bubble in field_block_bubbles:
                centre = (
                    int(bubble.x - x0 + box_w / 2),
                    int(bubble.y - y0 + box_h / 2),
                )
                axes = (max(int(box_w / 2) - 1, 1), max(int(box_h / 2) - 1, 1))
                cv2.ellipse(mask, centre, axes, 0, 0, 360, 1.0, 2)
        if (
            not mask.any()
            or mask.shape[0] > region.shape[0]
            or mask.shape[1] > region.shape[1]
        ):
            return field_block.shift, 0
        scores = cv2.matchTemplate(region, mask, cv2.TM_CCOEFF_NORMED)
        _, best, _, (best_x, best_y) = cv2.minMaxLoc(scores)
        centre_score = scores[radius, radius]
        dx, dy = best_x - radius, best_y - radius
        # Only move when the fit is clearly better than staying put, and never to
        # the edge of the search window (the true optimum may lie beyond it)
        if best - centre_score < 0.02 or abs(dx) == radius or abs(dy) == radius:
            return field_block.shift, 0
        return dx, dy

    def get_model_marked_probs(self, img, template):
        """Probability that each bubble is marked, in template traversal order."""
        if self.bubble_classifier is None:
            return None
        crops = []
        for field_block in template.field_blocks:
            box_w, box_h = field_block.bubble_dimensions
            for field_block_bubbles in field_block.traverse_bubbles:
                for bubble in field_block_bubbles:
                    x = bubble.x + field_block.shift + bubble.dx
                    y = bubble.y + field_block.shift_y + bubble.dy
                    crops.append(img[max(y, 0) : y + box_h, max(x, 0) : x + box_w])
        probabilities = self.bubble_classifier.predict_proba(crops)
        return probabilities[:, self.bubble_classifier.label_index("marked")]

    @staticmethod
    def get_fill_ratio(img, x, y, box_w, box_h, threshold):
        """Fraction of dark pixels inside the bubble's inscribed ellipse.

        Ignores the box corners and most of the printed outline, so it measures
        how much of the bubble interior was actually filled in.
        """
        roi = img[max(y, 0) : y + box_h, max(x, 0) : x + box_w]
        if roi.size == 0:
            return 0.0
        h, w = roi.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.ellipse(
            mask,
            (w // 2, h // 2),
            (max(int(w * 0.35), 1), max(int(h * 0.35), 1)),
            0,
            0,
            360,
            255,
            -1,
        )
        inside = roi[mask > 0]
        if inside.size == 0:
            return 0.0
        return float(np.count_nonzero(inside < threshold)) / float(inside.size)

    @staticmethod
    def summarize_field(
        field_label,
        value,
        bubble_details,
        marked_count,
        low_confidence,
        params,
        extra_flags=None,
    ):
        flags = list(extra_flags or [])
        if marked_count > 1:
            flags.append("multi_marked")
        if marked_count == 0:
            flags.append("empty")
        if low_confidence:
            flags.append("ambiguous_threshold")
        for bubble in bubble_details:
            if bubble["marked"] and bubble["fill_ratio"] < params.min_marked_fill_ratio:
                flags.append("weak_mark")
            if (
                not bubble["marked"]
                and bubble["fill_ratio"] > params.max_unmarked_fill_ratio
            ):
                flags.append("possible_missed_mark")
            if bubble.get("model_disagrees"):
                flags.append("model_disagrees")
        confidence = min((b["confidence"] for b in bubble_details), default=0.0)
        if confidence < params.min_confidence:
            flags.append("low_confidence")
        flags = sorted(set(flags))
        return {
            "label": field_label,
            "value": value,
            "confidence": round(float(confidence), 3),
            "flags": flags,
            "needs_review": any(flag in params.review_flags for flag in flags),
            "bubbles": bubble_details,
        }

    @staticmethod
    def draw_template_layout(img, template, shifted=True, draw_qvals=False, border=-1):
        img = ImageUtils.resize_util(
            img, template.page_dimensions[0], template.page_dimensions[1]
        )
        final_align = img.copy()
        for field_block in template.field_blocks:
            s, d = field_block.origin, field_block.dimensions
            box_w, box_h = field_block.bubble_dimensions
            shift = field_block.shift
            if shifted:
                cv2.rectangle(
                    final_align,
                    (s[0] + shift, s[1]),
                    (s[0] + shift + d[0], s[1] + d[1]),
                    CLR_BLACK,
                    3,
                )
            else:
                cv2.rectangle(
                    final_align,
                    (s[0], s[1]),
                    (s[0] + d[0], s[1] + d[1]),
                    CLR_BLACK,
                    3,
                )
            for field_block_bubbles in field_block.traverse_bubbles:
                for pt in field_block_bubbles:
                    x, y = (
                        (
                            pt.x + field_block.shift + pt.dx,
                            pt.y + getattr(field_block, "shift_y", 0) + pt.dy,
                        )
                        if shifted
                        else (pt.x, pt.y)
                    )
                    cv2.rectangle(
                        final_align,
                        (int(x + box_w / 10), int(y + box_h / 10)),
                        (int(x + box_w - box_w / 10), int(y + box_h - box_h / 10)),
                        CLR_GRAY,
                        border,
                    )
                    if draw_qvals:
                        rect = [y, y + box_h, x, x + box_w]
                        cv2.putText(
                            final_align,
                            f"{int(cv2.mean(img[rect[0] : rect[1], rect[2] : rect[3]])[0])}",
                            (rect[2] + 2, rect[0] + (box_h * 2) // 3),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.6,
                            CLR_BLACK,
                            2,
                        )
            if shifted:
                text_in_px = cv2.getTextSize(
                    field_block.name, cv2.FONT_HERSHEY_SIMPLEX, TEXT_SIZE, 4
                )
                cv2.putText(
                    final_align,
                    field_block.name,
                    (int(s[0] + d[0] - text_in_px[0][0]), int(s[1] - text_in_px[0][1])),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    TEXT_SIZE,
                    CLR_BLACK,
                    4,
                )
        return final_align

    def get_global_threshold(
        self,
        q_vals_orig,
        plot_title=None,
        plot_show=True,
        sort_in_plot=True,
        looseness=1,
    ):
        """
        Note: Cannot assume qStrip has only-gray or only-white bg
            (in which case there is only one jump).
        So there will be either 1 or 2 jumps.
        1 Jump :
                ......
                ||||||
                ||||||  <-- risky THR
                ||||||  <-- safe THR
            ....||||||
            ||||||||||

        2 Jumps :
                ......
                |||||| <-- wrong THR
            ....||||||
            |||||||||| <-- safe THR
            ..||||||||||
            ||||||||||||

        The abstract "First LARGE GAP" is perfect for this.
        Current code is considering ONLY TOP 2 jumps(>= MIN_GAP) to be big,
            gives the smaller one

        """
        config = self.tuning_config
        PAGE_TYPE_FOR_THRESHOLD, MIN_JUMP, JUMP_DELTA = map(
            config.threshold_params.get,
            [
                "PAGE_TYPE_FOR_THRESHOLD",
                "MIN_JUMP",
                "JUMP_DELTA",
            ],
        )

        global_default_threshold = (
            GLOBAL_PAGE_THRESHOLD_WHITE
            if PAGE_TYPE_FOR_THRESHOLD == "white"
            else GLOBAL_PAGE_THRESHOLD_BLACK
        )

        # Sort the Q bubbleValues
        # TODO: Change var name of q_vals
        q_vals = sorted(q_vals_orig)
        # Find the FIRST LARGE GAP and set it as threshold:
        ls = (looseness + 1) // 2
        l = len(q_vals) - ls
        max1, thr1 = MIN_JUMP, global_default_threshold
        for i in range(ls, l):
            jump = q_vals[i + ls] - q_vals[i - ls]
            if jump > max1:
                max1 = jump
                thr1 = q_vals[i - ls] + jump / 2

        # NOTE: thr2 is deprecated, thus is JUMP_DELTA
        # Make use of the fact that the JUMP_DELTA(Vertical gap ofc) between
        # values at detected jumps would be atleast 20
        max2, thr2 = MIN_JUMP, global_default_threshold
        # Requires atleast 1 gray box to be present (Roll field will ensure this)
        for i in range(ls, l):
            jump = q_vals[i + ls] - q_vals[i - ls]
            new_thr = q_vals[i - ls] + jump / 2
            if jump > max2 and abs(thr1 - new_thr) > JUMP_DELTA:
                max2 = jump
                thr2 = new_thr
        if max1 == MIN_JUMP and len(q_vals) >= 4:
            # No single large jump (e.g. pencil and partial marks fill the gap between
            # empty and dark bubbles): fall back to Otsu's two-class split instead of
            # a fixed page-type default, which can mark every bubble on light scans
            otsu_thr = self.otsu_threshold(q_vals)
            if otsu_thr is not None:
                thr1 = otsu_thr
        # global_thr = min(thr1,thr2)
        global_thr, j_low, j_high = thr1, thr1 - max1 // 2, thr1 + max1 // 2

        # # For normal images
        # thresholdRead =  116
        # if(thr1 > thr2 and thr2 > thresholdRead):
        #     print("Note: taking safer thr line.")
        #     global_thr, j_low, j_high = thr2, thr2 - max2//2, thr2 + max2//2

        if plot_title:
            _, ax = plt.subplots()
            ax.bar(range(len(q_vals_orig)), q_vals if sort_in_plot else q_vals_orig)
            ax.set_title(plot_title)
            thrline = ax.axhline(global_thr, color="green", ls="--", linewidth=5)
            thrline.set_label("Global Threshold")
            thrline = ax.axhline(thr2, color="red", ls=":", linewidth=3)
            thrline.set_label("THR2 Line")
            # thrline=ax.axhline(j_low,color='red',ls='-.', linewidth=3)
            # thrline=ax.axhline(j_high,color='red',ls='-.', linewidth=3)
            # thrline.set_label("Boundary Line")
            # ax.set_ylabel("Mean Intensity")
            ax.set_ylabel("Values")
            ax.set_xlabel("Position")
            ax.legend()
            if plot_show:
                plt.title(plot_title)
                plt.show()

        return global_thr, j_low, j_high

    @staticmethod
    def otsu_threshold(values, min_separation=30):
        """Otsu split of 1-D intensities; None if the classes aren't clearly apart."""
        values = np.sort(np.asarray(values, dtype=np.float64))
        n = len(values)
        best_score, best_index = -1.0, None
        cumulative = np.cumsum(values)
        total = cumulative[-1]
        for i in range(1, n):
            w0, w1 = i / n, (n - i) / n
            mean0 = cumulative[i - 1] / i
            mean1 = (total - cumulative[i - 1]) / (n - i)
            score = w0 * w1 * (mean0 - mean1) ** 2
            if score > best_score:
                best_score, best_index = score, i
        if best_index is None:
            return None
        mean0 = values[:best_index].mean()
        mean1 = values[best_index:].mean()
        if mean1 - mean0 < min_separation:
            return None
        return float((values[best_index - 1] + values[best_index]) / 2)

    def get_local_threshold(
        self, q_vals, global_thr, no_outliers, plot_title=None, plot_show=True
    ):
        """
        TODO: Update this documentation too-
        //No more - Assumption : Colwise background color is uniformly gray or white,
                but not alternating. In this case there is atmost one jump.

        0 Jump :
                        <-- safe THR?
            .......
            ...|||||||
            ||||||||||  <-- safe THR?
        // How to decide given range is above or below gray?
            -> global q_vals shall absolutely help here. Just run same function
                on total q_vals instead of colwise _//
        How to decide it is this case of 0 jumps

        1 Jump :
                ......
                ||||||
                ||||||  <-- risky THR
                ||||||  <-- safe THR
            ....||||||
            ||||||||||

        """
        config = self.tuning_config
        # Set when the strip has no clear jump and can't fall back to the global threshold
        self.last_local_threshold_low_confidence = False
        # Sort the Q bubbleValues
        q_vals = sorted(q_vals)

        # Small no of pts cases:
        # base case: 1 or 2 pts
        if len(q_vals) < 3:
            thr1 = (
                global_thr
                if np.max(q_vals) - np.min(q_vals) < config.threshold_params.MIN_GAP
                else np.mean(q_vals)
            )
        else:
            # qmin, qmax, qmean, qstd = round(np.min(q_vals),2), round(np.max(q_vals),2),
            #   round(np.mean(q_vals),2), round(np.std(q_vals),2)
            # GVals = [round(abs(q-qmean),2) for q in q_vals]
            # gmean, gstd = round(np.mean(GVals),2), round(np.std(GVals),2)
            # # DISCRETION: Pretty critical factor in reading response
            # # Doesn't work well for small number of values.
            # DISCRETION = 2.7 # 2.59 was closest hit, 3.0 is too far
            # L2MaxGap = round(max([abs(g-gmean) for g in GVals]),2)
            # if(L2MaxGap > DISCRETION*gstd):
            #     no_outliers = False

            # # ^Stackoverflow method
            # print(field_label, no_outliers,"qstd",round(np.std(q_vals),2), "gstd", gstd,
            #   "Gaps in gvals",sorted([round(abs(g-gmean),2) for g in GVals],reverse=True),
            #   '\t',round(DISCRETION*gstd,2), L2MaxGap)

            # else:
            # Find the LARGEST GAP and set it as threshold: //(FIRST LARGE GAP)
            l = len(q_vals) - 1
            max1, thr1 = config.threshold_params.MIN_JUMP, 255
            for i in range(1, l):
                jump = q_vals[i + 1] - q_vals[i - 1]
                if jump > max1:
                    max1 = jump
                    thr1 = q_vals[i - 1] + jump / 2
            # print(field_label,q_vals,max1)

            confident_jump = (
                config.threshold_params.MIN_JUMP
                + config.threshold_params.CONFIDENT_SURPLUS
            )
            # If not confident, then only take help of global_thr
            if max1 < confident_jump:
                if no_outliers:
                    # All Black or All White case
                    thr1 = global_thr
                else:
                    # No clear jump yet the strip has outliers: the read is a guess
                    self.last_local_threshold_low_confidence = True

            # if(thr1 == 255):
            #     print("Warning: threshold is unexpectedly 255! (Outlier Delta issue?)",plot_title)

        # Make a common plot function to show local and global thresholds
        if plot_show and plot_title is not None:
            _, ax = plt.subplots()
            ax.bar(range(len(q_vals)), q_vals)
            thrline = ax.axhline(thr1, color="green", ls=("-."), linewidth=3)
            thrline.set_label("Local Threshold")
            thrline = ax.axhline(global_thr, color="red", ls=":", linewidth=5)
            thrline.set_label("Global Threshold")
            ax.set_title(plot_title)
            ax.set_ylabel("Bubble Mean Intensity")
            ax.set_xlabel("Bubble Number(sorted)")
            ax.legend()
            # TODO append QStrip to this plot-
            # appendSaveImg(6,getPlotImg())
            if plot_show:
                plt.show()
        return thr1

    def append_save_img(self, key, img):
        if self.save_image_level >= int(key):
            self.save_img_list[key].append(img.copy())

    def save_image_stacks(self, key, filename, save_dir):
        config = self.tuning_config
        if self.save_image_level >= int(key) and self.save_img_list[key] != []:
            name = os.path.splitext(filename)[0]
            result = np.hstack(
                tuple(
                    [
                        ImageUtils.resize_util_h(img, config.dimensions.display_height)
                        for img in self.save_img_list[key]
                    ]
                )
            )
            result = ImageUtils.resize_util(
                result,
                min(
                    len(self.save_img_list[key]) * config.dimensions.display_width // 3,
                    int(config.dimensions.display_width * 2.5),
                ),
            )
            ImageUtils.save_img(f"{save_dir}stack/{name}_{str(key)}_stack.jpg", result)

    def reset_all_save_img(self):
        for i in range(self.save_image_level):
            self.save_img_list[i + 1] = []
