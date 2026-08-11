# microservice-image — Test Report & Documentation

Generated from Allure results by `build_documentation.py` on 2026-08-11. Behaviors below are **verified by passing tests** — rerun the suite, rerun this script, and the document cannot drift from the code.

## 📊 Execution Summary

| Module | Total | Passed | Failed | Broken | Skipped | Duration |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| microservice-image | 30 | 30 | 0 | 0 | 0 | 92ms |

## 📝 Test Documentation (Behaviors)

This section describes the verified system behaviors based on passing tests.

### Epic: Infrastructure

#### Feature: HTTP contract

- **test_a_repeated_parameter_takes_its_first_occurrence**
- **test_bad_quality_is_400**
- **test_bad_quality_with_huge_declared_body_answers_and_closes**
- **test_blank_format_is_400_not_the_default**
- **test_blank_format_with_huge_declared_body_answers_and_closes**
- **test_blank_quality_is_400_not_the_default**
- **test_empty_content_length_is_400_with_a_visible_empty_value**
- **test_garbage_body_is_400**
- **test_get_404_closes_the_connection**
- **test_happy_path_png_to_webp**
- **test_health_is_up**
- **test_image_over_pixel_cap_is_400**
- **test_la_png_to_jpeg_is_flattened_not_a_torn_connection**
- **test_malformed_content_length_is_400**
- **test_missing_content_length_is_411**
- **test_omitted_parameters_still_mean_the_defaults**
- **test_out_of_range_quality_with_huge_declared_body_answers_and_closes**
- **test_oversized_declared_body_is_413_before_reading**
- **test_unsupported_format_is_400**
- **test_unsupported_format_with_huge_declared_body_answers_and_closes**
- **test_whitespace_is_padding_for_both_parameters**

### Epic: Use case

#### Feature: Encode image

- **test_a_non_whitelisted_decoder_is_refused**
- **test_a_save_failure_is_a_value_error_not_a_crash**
- **test_an_unreadable_image_is_refused**
- **test_an_unsupported_format_is_refused**
- **test_deterministic**
- **test_la_png_survives_jpeg_encoding**
- **test_quality_out_of_range_is_refused**
- **test_too_many_pixels_is_refused_before_load**
- **test_webp_is_a_valid_smaller_image**

