import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import image  # noqa: E402

SHA = 'a' * 40


def spec(**override):
    base = {
        'schema': 'image-spec.v1', 'image_tag': 'app-web:rc-{sha}', 'platform': 'linux/amd64', 'exclude': ['big/data'],
        'build_args': {'PUBLIC_URL': 'https://example.invalid', 'PUBLIC_KEY_ARG': '{public_key}', 'MODE': 'true'},
        'checks': {'uid': '1001', 'files': ['server.js', '.next/BUILD_ID'], 'node_check': ['server.js'], 'forbidden_dir': '.next/static',
                   'forbidden': ['SERVER_SECRET', 'DB_URL']},
        'manifest': {'platform': 'linux/amd64', 'mode': True, 'public_origin': 'https://example.invalid'},
        'signature': {'namespace': 'example-artifact', 'header': 'example-artifact-v1'},
        'publish': {'release_tag': 'web-artifacts', 'asset_prefix': 'web-', 'keep': 6},
    }
    base.update(override)
    return json.dumps(base).encode()


class SpecTests(unittest.TestCase):
    def test_valid_spec_is_normalized(self):
        s = image.load_spec(spec(), SHA)
        self.assertEqual(s['image_tag'], f'app-web:rc-{SHA}')
        self.assertEqual(s['checks']['forbidden'], ['SERVER_SECRET', 'DB_URL'])
        args = image.resolve_args(s['build_args'], SHA, 'pk_public')
        self.assertEqual(args['PUBLIC_KEY_ARG'], 'pk_public')
        with self.assertRaises(image.Failure):
            image.resolve_args(s['build_args'], SHA, '')

    def test_fail_closed(self):
        bad = [
            spec(extra=1),
            spec(schema='other'),
            spec(image_tag='app-web:latest'),
            spec(image_tag='App Web:{sha}'),
            spec(platform='windows/amd64'),
            spec(exclude=['../x']),
            spec(exclude=['/abs']),
            spec(build_args={'lower': 'x'}),
            spec(build_args={'A': '{secret}'}),
            spec(build_args={'A': 'two\nlines'}),
            spec(checks={'uid': 'root', 'files': ['a'], 'node_check': [], 'forbidden_dir': 's', 'forbidden': ['XYZ']}),
            spec(checks={'uid': '1', 'files': [], 'node_check': [], 'forbidden_dir': 's', 'forbidden': ['XYZ']}),
            spec(checks={'uid': '1', 'files': ['a'], 'node_check': [], 'forbidden_dir': 's', 'forbidden': ['X|Y']}),
            spec(manifest={'image_id': 'x'}),
            spec(manifest={'nested': {'a': 1}}),
            spec(signature={'namespace': 'Bad Space', 'header': 'h'}),
            spec(publish={'release_tag': 'r', 'asset_prefix': 'w-', 'keep': 0}),
            b'not json',
        ]
        for raw in bad:
            with self.subTest(raw=raw[:80]):
                with self.assertRaises(image.Failure):
                    image.load_spec(raw, SHA)
        with self.assertRaises(image.Failure):
            image.load_spec(spec(), 'abc')

    def test_manifest_and_message(self):
        s = image.load_spec(spec(), SHA)
        m = image.artifact_manifest(s, SHA, 'sha256:' + '1' * 64, '2' * 64, 'run')
        self.assertEqual(list(m)[:4], ['release_sha', 'image_id', 'image_tag', 'archive_sha256'])
        self.assertEqual((m['platform'], m['mode'], m['build_provenance']), ('linux/amd64', True, 'run'))
        self.assertEqual(image.signed_message('example-artifact-v1', SHA, m['image_id'], m['archive_sha256']),
                         f'example-artifact-v1\nrelease_sha: {SHA}\nimage_id: sha256:{"1" * 64}\narchive_sha256: {"2" * 64}\n'.encode())
        with self.assertRaises(image.Failure):
            image.signed_message('h', SHA, 'sha256:short', '2' * 64)


class StoreTests(unittest.TestCase):
    def test_stored_needs_all_three_uploaded(self):
        names = image.asset_names('web-', SHA)
        assets = [{'name': n, 'state': 'uploaded'} for n in names[:2]]
        self.assertFalse(image.stored(assets, 'web-', SHA))
        assets.append({'name': names[2], 'state': 'starter'})
        self.assertFalse(image.stored(assets, 'web-', SHA))
        assets[-1]['state'] = 'uploaded'
        self.assertTrue(image.stored(assets, 'web-', SHA))

    def test_prune_keeps_the_newest(self):
        assets, ident = [], 0
        for i, sha in enumerate(('a' * 40, 'b' * 40, 'c' * 40)):
            for name in image.asset_names('web-', sha):
                ident += 1
                assets.append({'id': ident, 'name': name, 'created_at': f'2026-01-0{i + 1}T00:00:00Z'})
        assets.append({'id': 99, 'name': 'unrelated.txt', 'created_at': '2020-01-01T00:00:00Z'})
        self.assertEqual(sorted(image.prune_plan(assets, 'web-', 2)), [1, 2, 3])
        self.assertEqual(image.prune_plan(assets, 'web-', 3), [])

    def test_summary(self):
        self.assertEqual(image.summarize({'stage': 'publish', 'state': 'success', 'image_id': 'sha256:' + 'f' * 64})[0], 'success')
        self.assertEqual(image.summarize({'stage': 'check', 'state': 'success', 'stored': True}), ('success', 'already stored'))
        self.assertEqual(image.summarize({'stage': 'check', 'state': 'success', 'stored': False})[0], 'error')
        self.assertEqual(image.summarize({'stage': 'build', 'state': 'failure'}), ('failure', 'failed at build'))
        self.assertEqual(image.summarize({}), ('error', 'incomplete after none'))


if __name__ == '__main__':
    unittest.main()
