import json
from pathlib import Path
import unittest
from unittest.mock import patch

from foreman.dispatch import InvalidOutput
from foreman.outcomes import Halt
from foreman.review_context import ReviewContext, ReviewSide, SCRIPTED_REVIEW_CONTEXT
from foreman.stage_output import ReviewOutput, parse_output
from foreman.workspace import WorktreeInspectionFailed
import test_review_rounds
from test_runner import APPROVE, FakeWorkspace, FINDING


def report(side, path, line):
    return {**APPROVE, 'findings': [{**FINDING, 'location': dict(side=side, path=path, line=line)}]}


class CitationTests(unittest.TestCase):
    def test_context_required(self):
        with self.assertRaisesRegex(InvalidOutput, 'trusted review context required'):
            parse_output(ReviewOutput, report('head', 'items.txt', 2))

    def test_invalid_location(self):
        for side, path, line, detail in (
            ('head', 'absent.txt', 1, 'does not exist'),
            ('head', 'items.txt', 4, 'is not a text line'),
            ('head', 'items.txt', 0, 'greater than 0'),
            ('previous', 'items.txt', 1, 'side'),
        ):
            with self.subTest(side=side, path=path, line=line), self.assertRaisesRegex(InvalidOutput, detail):
                parse_output(ReviewOutput, report(side, path, line), context=SCRIPTED_REVIEW_CONTEXT)

    def test_pinned_trees_cover_deletions_rename_sources_and_unchanged_paths(self):
        with test_review_rounds.review_trees() as (workspace, tip, base, _, head):
            context = workspace.review_context(tip, head)
            self.assertEqual(context.base.sha, base)
            for side, path, line in (('base', 'deleted.txt', 1), ('base', 'old.txt', 1),
                                      ('head', 'new.txt', 1), ('head', 'items.txt', 1)):
                with self.subTest(side=side, path=path):
                    parse_output(ReviewOutput, report(side, path, line), context=context)
            for side, path in (('head', 'deleted.txt'), ('base', 'new.txt'), ('base', 'docs/plus-sign-validation.txt')):
                with self.subTest(side=side, path=path), self.assertRaisesRegex(InvalidOutput, 'does not exist'):
                    parse_output(ReviewOutput, report(side, path, None), context=context)

    def test_binary_path_can_be_cited_without_a_line(self):
        side = ReviewSide('a'*40, frozenset({'binary'}), {}, frozenset({'binary'}))
        context = ReviewContext(side, side)
        parse_output(ReviewOutput, report('head', 'binary', None), context=context)
        with self.assertRaisesRegex(InvalidOutput, 'is not a text line'):
            parse_output(ReviewOutput, report('head', 'binary', 1), context=context)

    def test_cli_schema_acceptance_captures(self):
        for name in ('codex', 'claude', 'grok'):
            path = Path(__file__).with_name('fixtures') / 'review' / f'{name}-schema-probe.json'
            capture = json.loads(path.read_text())
            schema = capture['schema']['properties']['items']
            self.assertEqual((schema['minItems'], schema['items']['exclusiveMinimum']), (1, 0))
            if name == 'codex':
                events = [json.loads(line) for line in capture['stdout'].splitlines()]
                self.assertEqual(events[-1]['type'], 'turn.completed')
                result = json.loads(next(e['item']['text'] for e in events if e.get('item', {}).get('type') == 'agent_message'))
            else:
                envelope = json.loads(capture['stdout'])
                result = envelope['structured_output' if name == 'claude' else 'structuredOutput']
            self.assertEqual((capture['returncode'], result), (0, {'items': [1]}))


class CitationGraphTests(unittest.TestCase):
    run_rounds = test_review_rounds.ReviewRoundsTest.run_rounds

    def test_bad_finding_cannot_post_or_clear(self):
        result = self.run_rounds([report('head', 'absent.txt', 1)])
        self.assertEqual(result['halt'], Halt.invalid_output)
        self.assertIn('findings.0.location', result['halt_detail'])
        self.assertFalse(result["review"].review_receipt)
        self.assertEqual((self.forge.reviews, self.forge.merge_calls), ([], []))

    def test_later_rejection_keeps_only_prior_accepted_round(self):
        result = self.run_rounds([{**APPROVE, 'findings': [FINDING]}, report('head', 'items.txt', 99)])
        self.assertEqual(result['halt'], Halt.invalid_output)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual(len(result['review'].review_findings), 1)
        self.assertFalse(self.forge.merge_calls)

    def test_context_failure_and_empty_diff_do_not_dispatch_review(self):
        empty = ReviewSide('a'*40, frozenset(), {}, frozenset())
        for value in [WorktreeInspectionFailed('unreadable'), ReviewContext(empty, empty)]:
            with self.subTest(value=value), patch.object(FakeWorkspace, 'review_context',
                    side_effect=value if isinstance(value, Exception) else None, return_value=value):
                result = self.run_rounds([], rerun=True)
                self.assertEqual(result['halt'], Halt.git_failed)
                self.assertFalse([a for a in result['attempts'] if a['stage'] == 'review'])
                self.assertEqual((self.forge.reviews, self.forge.merge_calls), ([], []))
