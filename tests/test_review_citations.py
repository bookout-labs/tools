import json
from pathlib import Path
import unittest
from unittest.mock import patch

from foreman.dispatch import InvalidOutput
from foreman.review_context import ReviewContext, ReviewSide, SCRIPTED_REVIEW_CONTEXT
from foreman.stage_output import ReviewOutput, parse_output
from foreman.workspace import WorktreeInspectionFailed
import test_review_rounds
from test_runner import FakeWorkspace, FINDING

FIXTURES = Path(__file__).with_name('fixtures') / 'review'


def payload(name='grounded'):
    return json.loads((FIXTURES / f'{name}.json').read_text())


class CitationTests(unittest.TestCase):
    def parse(self, raw, context=SCRIPTED_REVIEW_CONTEXT):
        return parse_output(ReviewOutput, raw, context=context)

    def test_context_required(self):
        with self.assertRaises(InvalidOutput):
            parse_output(ReviewOutput, payload())

    def test_bad_fixtures_name_the_field(self):
        for name, field in [('placeholder-99', 'risk.alignment'), ('padded', 'risk.alignment.evidence'),
                            ('unrelated', 'risk.alignment.evidence'), ('stale', 'risk.alignment.evidence'),
                            ('invented-finding', 'findings.0.line')]:
            with self.subTest(name=name), self.assertRaisesRegex(InvalidOutput, field):
                self.parse(payload(name))

    def test_deleted_and_inapplicable_dimensions(self):
        raw = payload()
        dimension = raw['risk']['rollback']
        dimension.update(applies=False, assessment='Deleted obsolete file has no running service to roll back.',
                         evidence=[dict(side='base', path='deleted.txt', line=1)])
        output = self.parse(raw)
        self.assertIn('Not applicable.', output.risk.render())
        self.assertIn('base deleted.txt:1', output.risk.render())

    def test_rename_binary_mode_and_unchanged_dependency(self):
        context = ReviewContext(
            ReviewSide('a'*40, frozenset({'old', 'binary', 'mode', 'dep'}), {'old': 1, 'mode': 1, 'dep': 1}, frozenset({'old', 'binary', 'mode'})),
            ReviewSide('b'*40, frozenset({'new', 'binary', 'mode', 'dep'}), {'new': 1, 'mode': 1, 'dep': 1}, frozenset({'new', 'binary', 'mode'})))
        for side, path, line, valid in [('base', 'old', 1, True), ('head', 'old', 1, False),
                                      ('head', 'binary', None, True), ('head', 'binary', 1, False),
                                      ('head', 'mode', None, True)]:
            raw = payload()
            for name, dimension in raw['risk'].items():
                if name != 'level':
                    dimension['evidence'] = [dict(side=side, path=path, line=line), dict(side='head', path='dep', line=1)]
            raw['findings'] = [{**FINDING, 'file': 'dep', 'line': 1}]
            with self.subTest(side=side, path=path, line=line):
                if valid:
                    self.assertEqual(self.parse(raw, context).findings[0].file, 'dep')
                else:
                    with self.assertRaises(InvalidOutput):
                        self.parse(raw, context)


class CitationGraphTests(unittest.TestCase):
    run_rounds = test_review_rounds.ReviewRoundsTest.run_rounds

    def test_placeholder_rejected_before_post_or_merge(self):
        result = self.run_rounds([payload('placeholder-99')])
        self.assertEqual(result['halt'], 'invalid_output')
        self.assertIn('review ', result['halt_detail'])
        self.assertFalse(result.get('review_findings'))
        self.assertFalse(result.get('review_receipt'))
        self.assertFalse(self.forge.reviews)
        self.assertFalse(self.forge.merge_calls)
        self.assertEqual(result['attempts'][-1]['outcome'], 'ran')
        self.assertIsNotNone(result['attempts'][-1]['usage'])

    def test_later_rejection_keeps_only_prior_accepted_round(self):
        result = self.run_rounds([{**payload(), 'findings': [FINDING]}, payload('stale')])
        self.assertEqual(result['halt'], 'invalid_output')
        self.assertEqual(len(result['review_findings']), 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertFalse(result.get('review_receipt'))
        self.assertFalse(self.forge.merge_calls)

    def test_grounded_review_arms_merge(self):
        result = self.run_rounds([payload()])
        self.assertEqual(result['merge'], 'armed')
        self.assertIn('head items.txt:2', self.forge.reviews[0][3])

    def test_context_failure_and_empty_diff_do_not_dispatch_review(self):
        empty = ReviewSide('a'*40, frozenset(), {}, frozenset())
        for value in [WorktreeInspectionFailed('unreadable'), ReviewContext(empty, empty)]:
            with self.subTest(value=value), patch.object(FakeWorkspace, 'review_context', side_effect=value if isinstance(value, Exception) else None, return_value=value):
                result = self.run_rounds([])
                self.assertEqual(result['halt'], 'review_unavailable')
                self.assertNotIn('review', [p[0] for p in self.prompts])
                self.assertFalse(self.forge.reviews)
                self.assertFalse(self.forge.merge_calls)
