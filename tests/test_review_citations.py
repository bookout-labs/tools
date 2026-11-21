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
        for name, field, message in [('placeholder-99', 'risk.alignment', 'valid dictionary'),
                                     ('padded', 'risk.alignment.evidence', 'does not exist'),
                                     ('unrelated', 'risk.alignment.evidence', 'must cite a changed path'),
                                     ('stale', 'risk.alignment.evidence', 'is not a text line'),
                                     ('invented-finding', 'findings.0.line', 'greater than 0')]:
            with self.subTest(name=name), self.assertRaises(InvalidOutput) as raised:
                self.parse(payload(name))
            self.assertIn(field, str(raised.exception))
            self.assertIn(message, str(raised.exception))

    def test_bad_finding_path_names_its_index(self):
        raw = dict(payload(), findings=[FINDING, {**FINDING, 'id': 'bad', 'file': 'nope.py', 'line': 1}])
        with self.assertRaisesRegex(InvalidOutput, r"^findings\.1:.*nope.py.*does not exist"):
            self.parse(raw)

    def test_cli_schema_acceptance_captures(self):
        for name in ('codex', 'claude', 'grok'):
            capture = payload(name + '-schema-probe')
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
        self.assertEqual(result['halt'], Halt.invalid_output)
        self.assertIn('review claude-opus-5-5-high:', result['halt_detail'])
        self.assertFalse(result.get('review_findings'))
        self.assertFalse(result.get('review_receipt'))
        self.assertFalse(self.forge.reviews)
        self.assertFalse(self.forge.merge_calls)
        self.assertEqual((result['attempts'][-1]['outcome'], result['attempts'][-1]['usage']['input']), ('ran', 1))

    def test_later_rejection_keeps_only_prior_accepted_round(self):
        result = self.run_rounds([{**payload(), 'findings': [FINDING]}, payload('stale')])
        self.assertEqual(result['halt'], Halt.invalid_output)
        self.assertEqual(len(result['review_findings']), 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual(result['review_receipt'], {'id': 1, 'state': 'COMMENTED', 'commit_id': result['review_findings'][0]['reviewed_head']})
        self.assertFalse(self.forge.merge_calls)

    def test_grounded_review_arms_merge(self):
        result = self.run_rounds([payload()])
        self.assertEqual(result['merge'], 'armed')
        self.assertIn('head items.txt:2', self.forge.reviews[0][3])

    def test_context_failure_and_empty_diff_do_not_dispatch_review(self):
        empty = ReviewSide('a'*40, frozenset(), {}, frozenset())
        for value in [WorktreeInspectionFailed('unreadable'), ReviewContext(empty, empty)]:
            with self.subTest(value=value), patch.object(FakeWorkspace, 'review_context', side_effect=value if isinstance(value, Exception) else None, return_value=value):
                result = self.run_rounds([], rerun=True)
                self.assertEqual(result['halt'], Halt.review_unavailable)
                self.assertIsNone(result.get('pending_dispatch'))
                self.assertFalse([a for a in result['attempts'] if a['stage'] == 'review'])
                self.assertFalse(self.forge.reviews)
                self.assertFalse(self.forge.merge_calls)
