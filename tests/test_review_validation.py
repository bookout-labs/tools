import unittest

from foreman.review_validation import (
    annotate_unified_diff,
    build_diff_maps_from_annotated_diff,
    build_diff_hunk_maps,
    validate_review_comments,
    validate_review_payload,
)

SAMPLE_DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,4 +10,5 @@ def compute(x):
     base = x + 1
-    return base * 2
+    scaled = base * 2
+    return scaled
     # end
\\ No newline at end of file
diff --git a/src/removed.py b/src/removed.py
deleted file mode 100644
--- a/src/removed.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old_line = 1
-other = 2
"""


class ReviewValidationTest(unittest.TestCase):
    def test_header_like_hunk_lines_and_literal_directory_prefixes(self):
        sample = """--- a/a/app.py
+++ b/a/app.py
@@ -1 +1,2 @@
--- removed
+++ added
+another line
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""
        annotated = annotate_unified_diff(sample)
        line_map, content_map = build_diff_maps_from_annotated_diff(annotated)
        self.assertEqual(content_map["a/app.py"]["LEFT"][1], "-- removed")
        self.assertEqual(content_map["a/app.py"]["RIGHT"][1], "++ added")
        comments = [
            {"path": "./a/app.py", "line": 2, "side": "RIGHT", "body": "literal directory"},
            {"path": "./b/app.py", "line": 1, "side": "RIGHT", "body": "diff prefix"},
        ]
        result = validate_review_comments(comments, line_map)
        self.assertEqual([item["path"] for item in result.comments], ["a/app.py", "app.py"])

    def test_git_quoted_paths_with_utf8_octal_escapes_are_decoded(self):
        diff = (
            'diff --git "a/src/caf\\303\\251.py" "b/src/caf\\303\\251.py"\n'
            '--- "a/src/caf\\303\\251.py"\n'
            '+++ "b/src/caf\\303\\251.py"\n'
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        line_map, _ = build_diff_maps_from_annotated_diff(annotate_unified_diff(diff))
        self.assertEqual(list(line_map), ["src/café.py"])
        self.assertEqual(list(build_diff_hunk_maps(diff)), ["src/café.py"])
        tab_diff = '--- "a/a\\tb"\n+++ "b/a\\tb"\n@@ -1 +1 @@\n-x\n+y\n'
        self.assertEqual(list(build_diff_hunk_maps(tab_diff)), ["a\tb"])

    def test_multiline_anchors_must_be_in_the_same_hunk(self):
        sample = """--- a/app.py
+++ b/app.py
@@ -1,0 +2,2 @@
+one
+two
@@ -20,0 +22 @@
+three
"""
        line_map, _ = build_diff_maps_from_annotated_diff(annotate_unified_diff(sample))
        comment = {"path": "app.py", "line": 3, "side": "RIGHT", "body": "Range",
                   "start_line": 2, "start_side": "RIGHT"}
        result = validate_review_comments(
            [comment, {**comment, "line": 22}], line_map, diff_hunk_map=build_diff_hunk_maps(sample),
        )
        self.assertEqual(result.comments, [comment])
        self.assertIn("same hunk", result.errors[0])

    def test_annotate_and_build_maps_for_modified_and_deleted_files(self):
        annotated = annotate_unified_diff(SAMPLE_DIFF)
        self.assertIn("[OLD:10,NEW:10]     base = x + 1", annotated)
        self.assertIn("[OLD:11]     return base * 2", annotated)
        self.assertIn("[NEW:11]     scaled = base * 2", annotated)
        self.assertIn("[NEW:12]     return scaled", annotated)
        self.assertIn("[OLD:1] old_line = 1", annotated)

        line_map, content_map = build_diff_maps_from_annotated_diff(annotated)
        self.assertEqual(line_map["src/app.py"]["LEFT"], {10, 11, 12})
        self.assertEqual(line_map["src/app.py"]["RIGHT"], {10, 11, 12, 13})
        self.assertEqual(content_map["src/app.py"]["RIGHT"][11], "    scaled = base * 2")
        self.assertEqual(line_map["src/removed.py"]["LEFT"], {1, 2})
        self.assertEqual(line_map["src/removed.py"]["RIGHT"], set())

    def test_validate_comments_checks_hunk_membership_sides_and_ranges(self):
        annotated = annotate_unified_diff(SAMPLE_DIFF)
        line_map, content_map = build_diff_maps_from_annotated_diff(annotated)

        valid = validate_review_comments(
            [
                {"path": "b/src/app.py", "line": 11, "side": "RIGHT", "body": "Check overflow."},
                {
                    "path": "src/app.py",
                    "start_line": 11,
                    "start_side": "RIGHT",
                    "line": 12,
                    "side": "RIGHT",
                    "body": "```suggestion\n    scaled = base * 3\n    return scaled\n```",
                },
                {"path": "src/removed.py", "line": 1, "side": "LEFT", "body": "Verify removal."},
            ],
            line_map,
            content_map,
        )
        self.assertTrue(valid.is_valid, valid.errors)
        self.assertEqual(len(valid.comments), 3)
        self.assertEqual(valid.comments[0]["path"], "src/app.py")

        invalid = validate_review_comments(
            [
                {"path": "src/missing.py", "line": 1, "side": "RIGHT", "body": "x"},
                {"path": "src/app.py", "line": 99, "side": "RIGHT", "body": "x"},
                {"path": "src/app.py", "line": 13, "side": "LEFT", "body": "wrong side"},
                {"path": "src/app.py", "line": 11, "side": "RIGHT", "start_line": 12, "start_side": "RIGHT", "body": "bad range"},
                {"path": "src/app.py", "line": True, "side": "RIGHT", "body": "bool line"},
            ],
            line_map,
            content_map,
        )
        self.assertFalse(invalid.is_valid)
        self.assertEqual(len(invalid.errors), 5)
        self.assertEqual(invalid.comments, [])

    def test_validate_suggestion_rejects_duplicated_surrounding_context(self):
        annotated = annotate_unified_diff(SAMPLE_DIFF)
        line_map, content_map = build_diff_maps_from_annotated_diff(annotated)

        payload_res = validate_review_payload(
            {
                "body": "  Review summary  ",
                "comments": [
                    {
                        "path": "src/app.py",
                        "line": 11,
                        "side": "RIGHT",
                        "body": "```suggestion\n    base = x + 1\n    scaled = base * 3\n```",
                    },
                    {
                        "path": "src/app.py",
                        "line": 12,
                        "side": "RIGHT",
                        "body": "```suggestion\n    return scaled + 1\n    # end\n```",
                    },
                ],
            },
            line_map,
            content_map,
        )
        self.assertEqual(payload_res.body, "Review summary")
        self.assertFalse(payload_res.is_valid)
        self.assertEqual(len(payload_res.errors), 2)
        self.assertIn("context line 10 above start_line", payload_res.errors[0])
        self.assertIn("context line 13 below line", payload_res.errors[1])


if __name__ == "__main__":
    unittest.main()
