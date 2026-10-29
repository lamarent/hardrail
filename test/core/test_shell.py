import unittest

from core.shell import ParseError, split_commands


class SplitTests(unittest.TestCase):
    def argvs(self, cmd):
        return [s.argv for s in split_commands(cmd)]

    def test_single(self):
        self.assertEqual(self.argvs('pnpm test'), [['pnpm', 'test']])

    def test_quote_splice(self):
        self.assertEqual(self.argvs('p"n"pm test'), [['pnpm', 'test']])

    def test_escaped_quote_not_eaten(self):
        self.assertEqual(self.argvs('echo "\\"" ; pnpm test ; echo "\\""')[1], ['pnpm', 'test'])

    def test_newline_and_ampersand(self):
        self.assertEqual(self.argvs('git commit -m x\ngit push'), [['git', 'commit', '-m', 'x'], ['git', 'push']])
        self.assertEqual(self.argvs('git commit -m x & git push'), [['git', 'commit', '-m', 'x'], ['git', 'push']])

    def test_sh_c_payload(self):
        s = split_commands('bash -c "pnpm test"')
        self.assertEqual(s[-1].argv, ['pnpm', 'test'])
        self.assertEqual(s[-1].payload_of, 'sh -c')

    def test_heredoc_into_bash(self):
        s = split_commands('bash <<EOF\npnpm test\nEOF')
        self.assertEqual(s[-1].argv, ['pnpm', 'test'])
        self.assertEqual(s[-1].payload_of, 'heredoc')

    def test_heredoc_into_cat_dropped(self):
        self.assertEqual(self.argvs('cat <<EOF\npnpm test\nEOF'), [['cat']])

    def test_find_exec_semicolon(self):
        self.assertEqual(len(split_commands('find . -name "*.orig" -exec rm {} \;')), 1)

    def test_docker_sh_c_payload_separate(self):
        s = split_commands('docker run --rm alpine sh -c "ls; pwd"')
        self.assertEqual(s[0].argv[:2], ['docker', 'run'])
        self.assertEqual([x.argv for x in s[1:]], [['ls'], ['pwd']])

    def test_non_ascii(self):
        self.assertEqual(self.argvs('git commit -m "修正"'), [['git', 'commit', '-m', '修正']])

    def test_unbalanced_raises(self):
        with self.assertRaises(ParseError):
            split_commands('pnpm test "oops')

    def test_extra(self):
        self.assertEqual(self.argvs('a && b || c | d'), [['a'], ['b'], ['c'], ['d']])
        self.assertEqual(self.argvs('make 2>&1 | tee x'), [['make', '2>&1'], ['tee', 'x']])
        self.assertEqual(self.argvs('echo "a;b"'), [['echo', 'a;b']])
        s = split_commands('eval "pnpm test"')
        self.assertEqual((s[-1].argv, s[-1].payload_of), (['pnpm', 'test'], 'eval'))
        s = split_commands("bash -lc 'git push'")
        self.assertEqual(s[-1].argv, ['git', 'push'])


if __name__ == '__main__':
    unittest.main()
