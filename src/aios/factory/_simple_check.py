"""Fixed stdlib-only unittest bootstrap. Developer code is explicitly trusted."""
from pathlib import Path
import hashlib
import importlib
import importlib.machinery
import json
import os
import stat
import sys
import unittest


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


WINDOWS_STDLIB_EXTENSIONS = sys.stdlib_module_names


def windows_extension_file(name, base_exec_prefix, enabled):
    if not enabled or name not in WINDOWS_STDLIB_EXTENSIONS or name not in sys.stdlib_module_names:
        return None
    try:
        source = (Path(base_exec_prefix) / 'DLLs' / (name + '.pyd')).absolute()
        for part in (*reversed(source.parents), source):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
        before = source.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        checksum = sha(source)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink):
            return None
        return {'path': str(source), 'sha256': checksum}
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def windows_extension_origin(name, module, base_exec_prefix, enabled):
    record = windows_extension_file(name, base_exec_prefix, enabled)
    if record is None:
        return None
    try:
        source = Path(module.__file__).absolute()
        spec = module.__spec__
        if (os.path.normcase(str(source)) != os.path.normcase(record['path']) or
                not isinstance(spec.loader, importlib.machinery.ExtensionFileLoader) or
                os.path.normcase(os.path.abspath(spec.origin)) != os.path.normcase(record['path'])):
            return None
        return record
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def windows_hashlib_origin(name, module, base_exec_prefix, enabled):
    return windows_extension_origin(name, module, base_exec_prefix, enabled) if name == '_hashlib' else None


def importable_file_name(name):
    """Return a top-level import name for a suffix accepted by this Python."""
    for suffix in sorted(importlib.machinery.all_suffixes(), key=len, reverse=True):
        if name.casefold().endswith(suffix.casefold()):
            stem = name[:-len(suffix)]
            return stem.casefold() if stem.isidentifier() else None
    return None


def dependency_module_roots(inventory):
    names = set()
    for row in inventory:
        path = row['path']; parts = path.split('/')
        if importable_file_name(parts[-1]) is None:
            continue
        if len(parts) == 1:
            name = importable_file_name(parts[0])
        else:
            name = parts[0].casefold() if parts[0].isidentifier() else None
        if name is not None: names.add(name)
    return names


def run(root, request):
    root = Path(root).resolve()
    source = (root / request.get('source_root', '.')).resolve()
    if not source.is_relative_to(root) or not source.is_dir():
        raise ValueError('source_root')
    dependency = request.get('dependencies')
    dep_root = Path(dependency['root']).resolve() if dependency is not None else None
    if dep_root is not None and (dep_root.is_relative_to(root) or root.is_relative_to(dep_root)):
        raise ValueError('dependency_overlap')
    def dependency_inventory():
        if dep_root is None:
            return []
        result = []; names = set(); total = 0
        for base, dirs, files in os.walk(dep_root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            for name in sorted(dirs + files):
                p = Path(base) / name; rel = p.relative_to(dep_root).as_posix(); info = p.lstat()
                if (rel.casefold() in names or any(x.casefold() == '.git' or x == '__pycache__' for x in rel.split('/')) or
                    stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or
                    not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                    raise ValueError('dependency_inventory')
                names.add(rel.casefold())
                if stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1 or rel.lower().endswith(('.pth', '.pyc')):
                        raise ValueError('dependency_inventory')
                    total += info.st_size
                    if total > 100 * 1024 * 1024 or len(result) >= 10000:
                        raise ValueError('dependency_inventory')
                    result.append({'path': rel, 'mode': '100755' if os.name != 'nt' and info.st_mode & 0o111 else '100644',
                                   'bytes': info.st_size, 'sha256': sha(p)})
        return sorted(result, key=lambda row: row['path'].encode())
    dep_before = dependency_inventory()
    if dependency is not None and dep_before != dependency['files']:
        raise ValueError('dependency_drift')
    dependency_names = dependency_module_roots(dep_before)
    for base in {root, source}:
        for entry in base.iterdir():
            top = entry.name.casefold() if entry.is_dir() and entry.name.isidentifier() else importable_file_name(entry.name)
            if top in dependency_names:
                raise ValueError('dependency_shadow')
    def inventory():
        found = {}
        for base, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            for name in sorted(dirs + files):
                path = Path(base) / name; info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or
                        not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                    raise ValueError('candidate_inventory')
                if stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1: raise ValueError('candidate_inventory')
                    found[path.relative_to(root).as_posix()] = {'sha256': sha(path), 'bytes': info.st_size}
        return found
    before = inventory()
    enabled = sys.platform == 'win32'
    extension_before = {name: record for name in WINDOWS_STDLIB_EXTENSIONS
                        if (record := windows_extension_file(name, sys.base_exec_prefix, enabled)) is not None}
    invalid_extension_before = any(name in sys.modules and isinstance(getattr(sys.modules[name], '__file__', None), str) and
        Path(sys.modules[name].__file__).suffix.casefold() == '.pyd' and
        (windows_extension_origin(name, sys.modules[name], sys.base_exec_prefix, enabled) is None or
         windows_extension_origin(name, sys.modules[name], sys.base_exec_prefix, enabled) != extension_before.get(name))
        for name in WINDOWS_STDLIB_EXTENSIONS) if enabled else False
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    if source != root: sys.path.insert(1, str(source))
    if dep_root is not None: sys.path.insert(2 if source != root else 1, str(dep_root))
    modules = {}
    for name in request['target_modules'] + request['test_modules']:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        base = source if name in request['target_modules'] else root
        stem = base.joinpath(*name.split('.'))
        choices = [p for p in (stem.with_suffix('.py'), stem / '__init__.py') if p.is_file()]
        if len(choices) != 1 or path != choices[0]:
            raise ValueError('module_origin_mismatch')
        modules[name] = {'path': str(path), 'sha256': sha(path)}
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[name])
                               for name in request['test_modules'])
    class Result(unittest.TextTestResult):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.ids = []; self.subtests = 0; self.bad_subtests = 0
        def startTest(self, test):
            self.ids.append(test.id()); super().startTest(test)
        def addSubTest(self, test, subtest, error):
            self.subtests += 1; self.bad_subtests += error is not None
            super().addSubTest(test, subtest, error)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    origins = {}
    unexpected = {}
    dependency_origins = {}
    dep_files = {r['path']: r for r in dep_before}
    target_names = {name.split('.')[0] for name in request['target_modules']}
    extension_after = {name: windows_extension_file(name, sys.base_exec_prefix, enabled)
                       for name in extension_before}
    stdlib = Path(os.__file__).resolve().parent
    for name, module in sorted(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if not filename:
            locations = getattr(module, '__path__', None)
            resolved = [Path(p).resolve() for p in locations] if locations else []
            in_dependency = [p for p in resolved if dep_root is not None and p.is_relative_to(dep_root)]
            if in_dependency:
                if (len(in_dependency) != len(resolved) or any(not p.is_dir() or
                    not any(row['path'].startswith(p.relative_to(dep_root).as_posix() + '/') for row in dep_before)
                    for p in resolved)):
                    unexpected[name] = 'mixed_dependency_namespace'
                else:
                    dependency_origins[name] = {'namespace_paths': [str(p) for p in resolved]}
            elif name.split('.')[0].casefold() in dependency_names:
                unexpected[name] = 'dependency_origin_unavailable'
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(root):
            path = Path(filename).resolve()
            if (name.split('.')[0].casefold() in dependency_names or
                    (name.split('.')[0] in target_names and not path.is_relative_to(source))):
                unexpected[name] = str(path)
            else:
                origins[name] = {'path': str(path), 'sha256': sha(path)}
        elif dep_root is not None and path.is_relative_to(dep_root):
            rel = path.relative_to(dep_root).as_posix(); record = dep_files.get(rel)
            if name.split('.')[0] in target_names or record is None or sha(path) != record['sha256']:
                unexpected[name] = str(path)
            else:
                dependency_origins[name] = {'path': str(path), 'sha256': record['sha256']}
        elif name.split('.')[0].casefold() in dependency_names:
            unexpected[name] = str(path)
        elif name in extension_before and windows_extension_origin(name, module, sys.base_exec_prefix, enabled) == extension_before[name] == extension_after[name]:
            continue
        elif path != Path(__file__).resolve() and (not path.is_relative_to(stdlib) or 'site-packages' in path.parts):
            unexpected[name] = str(path)
    after = inventory()
    dep_after = dependency_inventory()
    passed = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped and
              not result.expectedFailures and not result.unexpectedSuccesses and
              len(result.ids) == len(set(result.ids)) and
              set(request['required_test_ids']) <= set(result.ids) and
              all(name in origins and origins[name] == value for name, value in modules.items()) and
              before == after and dep_before == dep_after and not unexpected and not invalid_extension_before and
              extension_before == extension_after)
    return {'kind': 'factory-simple-test-v1', 'passed': passed, 'run': result.testsRun,
            'ids': result.ids, 'failures': len(result.failures), 'errors': len(result.errors),
            'skips': len(result.skipped), 'expected_failures': len(result.expectedFailures),
            'unexpected_successes': len(result.unexpectedSuccesses),
            'subtests': result.subtests, 'failed_subtests': result.bad_subtests,
            'modules': modules, 'candidate_imports': origins,
            'source_before': before, 'source_after': after, 'source_unchanged': before == after,
            'unexpected_origins': unexpected, 'sys_path': list(sys.path),
            'dependency_origins': dependency_origins, 'dependency_before': dep_before,
            'dependency_after': dep_after, 'dependency_unchanged': dep_before == dep_after,
            'trusted_windows_extensions_before': extension_before,
            'trusted_windows_extensions': extension_after,
            'trusted_windows_extensions_unchanged': extension_before == extension_after,
            'invalid_windows_extension_before': invalid_extension_before,
            'python': {'path': sys.executable, 'sha256': sha(sys.executable), 'version': sys.version,
                       'base_exec_prefix': str(Path(sys.base_exec_prefix).resolve())},
            'stdlib_unittest': {'path': unittest.__file__, 'sha256': sha(unittest.__file__)},
            'bootstrap': {'path': __file__, 'sha256': sha(__file__)}}


def main():
    root, request_path, result_path = map(Path, sys.argv[1:])
    result = run(root, json.loads(request_path.read_bytes()))
    with result_path.open('x') as stream:
        json.dump(result, stream, sort_keys=True, allow_nan=False)
        stream.write('\n')
    return 0 if result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
