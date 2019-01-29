#!/usr/bin/env python3
#############################################################################
##
## Copyright (C) 2018 The Qt Company Ltd.
## Contact: https://www.qt.io/licensing/
##
## This file is part of the plugins of the Qt Toolkit.
##
## $QT_BEGIN_LICENSE:GPL-EXCEPT$
## Commercial License Usage
## Licensees holding valid commercial Qt licenses may use this file in
## accordance with the commercial license agreement provided with the
## Software or, alternatively, in accordance with the terms contained in
## a written agreement between you and The Qt Company. For licensing terms
## and conditions see https://www.qt.io/terms-conditions. For further
## information use the contact form at https://www.qt.io/contact-us.
##
## GNU General Public License Usage
## Alternatively, this file may be used under the terms of the GNU
## General Public License version 3 as published by the Free Software
## Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
## included in the packaging of this file. Please review the following
## information to ensure the GNU General Public License requirements will
## be met: https://www.gnu.org/licenses/gpl-3.0.html.
##
## $QT_END_LICENSE$
##
#############################################################################


from __future__ import annotations

from argparse import ArgumentParser
import copy
import os.path
import re
import io
import typing

from sympy.logic import (simplify_logic, And, Or, Not,)
import pyparsing as pp

from helper import map_qt_library, map_qt_base_library, featureName, \
    substitute_platform, substitute_libs


def _parse_commandline():
    parser = ArgumentParser(description='Generate CMakeLists.txt files from .'
                            'pro files.')
    parser.add_argument('--debug', dest='debug', action='store_true',
                        help='Turn on all debug output')
    parser.add_argument('--debug-parser', dest='debug_parser',
                        action='store_true',
                        help='Print debug output from qmake parser.')
    parser.add_argument('--debug-parse-result', dest='debug_parse_result',
                        action='store_true',
                        help='Dump the qmake parser result.')
    parser.add_argument('--debug-parse-dictionary',
                        dest='debug_parse_dictionary', action='store_true',
                        help='Dump the qmake parser result as dictionary.')
    parser.add_argument('--debug-pro-structure', dest='debug_pro_structure',
                        action='store_true',
                        help='Dump the structure of the qmake .pro-file.')
    parser.add_argument('--debug-full-pro-structure',
                        dest='debug_full_pro_structure', action='store_true',
                        help='Dump the full structure of the qmake .pro-file '
                        '(with includes).')
    parser.add_argument('files', metavar='<.pro/.pri file>', type=str,
                        nargs='+', help='The .pro/.pri file to process')

    return parser.parse_args()


def spaces(indent: int) -> str:
    return '    ' * indent


def map_to_file(f: str, top_dir: str, current_dir: str,
                want_absolute_path: bool = False) -> typing.Optional[str]:
    if f == '$$NO_PCH_SOURCES':
        return None
    if f.startswith('$$PWD/') or f == '$$PWD':  # INCLUDEPATH += $$PWD
        return os.path.join(os.path.relpath(current_dir, top_dir), f[6:])
    if f.startswith('$$OUT_PWD/'):
        return "${CMAKE_CURRENT_BUILD_DIR}/" + f[10:]
    if f.startswith('$$QT_SOURCE_TREE'):
        return "${PROJECT_SOURCE_DIR}/" + f[17:]
    if f.startswith("./"):
        return os.path.join(current_dir, f)
    if want_absolute_path and not os.path.isabs(f):
        return os.path.join(current_dir, f)
    return f


def map_source_to_cmake(source: str, base_dir: str,
                        vpath: typing.List[str]) -> str:
    if not source or source == '$$NO_PCH_SOURCES':
        return ''
    if source.startswith('$$PWD/'):
        return source[6:]
    if source == '.':
        return "${CMAKE_CURRENT_SOURCE_DIR}"
    if source.startswith('$$QT_SOURCE_TREE/'):
        return "${PROJECT_SOURCE_DIR}/" + source[17:]

    if os.path.exists(os.path.join(base_dir, source)):
        return source

    for v in vpath:
        fullpath = os.path.join(v, source)
        if os.path.exists(fullpath):
            relpath = os.path.relpath(fullpath, base_dir)
            return relpath

    print('    XXXX: Source {}: Not found.'.format(source))
    return '{}-NOTFOUND'.format(source)


def map_source_to_fs(base_dir: str, file: str,
                     source: str) -> str:
    if source is None or source == '$$NO_PCH_SOURCES':
        return ''
    if source.startswith('$$PWD/'):
        return os.path.join(os.path.dirname(file), source[6:])
    if source.startswith('$$QT_SOURCE_TREE/'):
        return os.path.join('.', source[17:])
    if source.startswith('${PROJECT_SOURCE_DIR}/'):
        return os.path.join('.', source[22:])
    if source.startswith('${CMAKE_CURRENT_SOURCE_DIR}/'):
        return os.path.join(base_dir, source[28:])
    return os.path.join(base_dir, source)


class Operation:
    def __init__(self, value):
        if isinstance(value, list):
            self._value = value
        else:
            self._value = [str(value), ]

    def process(self, input):
        assert(False)

    def __repr__(self):
        assert(False)

    def _dump(self):
        if not self._value:
            return '<NOTHING>'

        if not isinstance(self._value, list):
            return '<NOT A LIST>'

        result = []
        for i in self._value:
            if not i:
                result.append('<NONE>')
            else:
                result.append(str(i))
        return '"' + '", "'.join(result) + '"'


class AddOperation(Operation):
    def process(self, input):
        return input + self._value

    def __repr__(self):
        return '+({})'.format(self._dump())


class UniqueAddOperation(Operation):
    def process(self, input):
        result = input
        for v in self._value:
            if v not in result:
                result += [v, ]
        return result

    def __repr__(self):
        return '*({})'.format(self._dump())


class SetOperation(Operation):
    def process(self, input):
        return self._value

    def __repr__(self):
        return '=({})'.format(self._dump())


class RemoveOperation(Operation):
    def __init__(self, value):
        super().__init__(value)

    def process(self, input):
        input_set = set(input)
        result = []
        for v in self._value:
            if v in input_set:
                continue
            else:
                result += ['-{}'.format(v), ]
        return result

    def __repr__(self):
        return '-({})'.format(self._dump())


class Scope(object):
    def __init__(self, *,
                 parent_scope: typing.Optional[Scope],
                 file: typing.Optional[str] = None, condition: str = '',
                 base_dir: str = '',
                 operations: typing.Mapping[str, typing.List[Operation]] = {}) -> None:
        if parent_scope:
            parent_scope._add_child(self)
        else:
            self._parent = None  # type: typing.Optional[Scope]

        self._basedir = base_dir
        if file:
            self._currentdir = os.path.dirname(file)
        if not self._currentdir:
            self._currentdir = '.'
        if not self._basedir:
            self._basedir = self._currentdir

        self._file = file
        self._condition = map_condition(condition)
        self._children = []  # type: typing.List[Scope]
        self._operations = copy.deepcopy(operations)
        self._visited_keys = set()  # type: typing.Set[str]
        self._total_condition = None  # type: typing.Optional[str]

    def __repr__(self):
        return '{}:{}:{}'.format(self._basedir, self._file,
                                 self._condition or '<NONE>')

    def reset_visited_keys(self):
        self._visited_keys = set()

    def merge(self, other: 'Scope') -> None:
        for c in other._children:
            self._add_child(c)

        for key in other._operations.keys():
            if key in self._operations:
                self._operations[key] += other._operations[key]
            else:
                self._operations[key] = other._operations[key]

    @property
    def parent(self) -> typing.Optional[Scope]:
        return self._parent

    @property
    def basedir(self) -> str:
        return self._basedir

    @property
    def currentdir(self) -> str:
        return self._currentdir

    @staticmethod
    def FromDict(parent_scope: typing.Optional['Scope'],
                 file: str, statements, cond: str = '', base_dir: str = ''):
        scope = Scope(parent_scope=parent_scope, file=file, condition=cond, base_dir=base_dir)
        for statement in statements:
            if isinstance(statement, list):  # Handle skipped parts...
                assert not statement
                continue

            operation = statement.get('operation', None)
            if operation:
                key = statement.get('key', '')
                value = statement.get('value', [])
                assert key != ''

                if key in ('HEADERS', 'SOURCES', 'INCLUDEPATH', 'RESOURCES',) \
                        or key.endswith('_HEADERS') \
                        or key.endswith('_SOURCES'):
                    value = [map_to_file(v, scope.basedir,
                                         scope.currentdir) for v in value]

                if operation == '=':
                    scope._append_operation(key, SetOperation(value))
                elif operation == '-=':
                    scope._append_operation(key, RemoveOperation(value))
                elif operation == '+=':
                    scope._append_operation(key, AddOperation(value))
                elif operation == '*=':
                    scope._append_operation(key, UniqueAddOperation(value))
                else:
                    print('Unexpected operation "{}" in scope "{}".'
                          .format(operation, scope))
                    assert(False)

                continue

            condition = statement.get('condition', None)
            if condition:
                Scope.FromDict(scope, file,
                               statement.get('statements'), condition,
                               scope.basedir)

                else_statements = statement.get('else_statements')
                if else_statements:
                    Scope.FromDict(scope, file, else_statements,
                                   'NOT ' + condition, scope.basedir)
                continue

            loaded = statement.get('loaded')
            if loaded:
                scope._append_operation('_LOADED', UniqueAddOperation(loaded))
                continue

            option = statement.get('option', None)
            if option:
                scope._append_operation('_OPTION', UniqueAddOperation(option))
                continue

            included = statement.get('included', None)
            if included:
                scope._append_operation('_INCLUDED',
                                        UniqueAddOperation(
                                            map_to_file(included,
                                                        scope.basedir,
                                                        scope.currentdir)))
                continue

        return scope

    def _append_operation(self, key: str, op: Operation) -> None:
        if key in self._operations:
            self._operations[key].append(op)
        else:
            self._operations[key] = [op, ]

    @property
    def file(self) -> str:
        return self._file or ''

    @property
    def cMakeListsFile(self) -> str:
        assert self.basedir
        return os.path.join(self.basedir, 'CMakeLists.txt')

    @property
    def condition(self) -> str:
        return self._condition

    @property
    def total_condition(self) -> typing.Optional[str]:
        return self._total_condition

    @total_condition.setter
    def total_condition(self, condition: str) -> None:
        self._total_condition = condition

    def _add_child(self, scope: 'Scope') -> None:
        scope._parent = self
        self._children.append(scope)

    @property
    def children(self) -> typing.List['Scope']:
        return self._children

    def dump(self, *, indent: int = 0) -> None:
        ind = '    ' * indent
        print('{}Scope "{}":'.format(ind, self))
        print('{}  Keys:'.format(ind))
        keys = self._operations.keys()
        if not keys:
            print('{}    -- NONE --'.format(ind))
        else:
            for k in sorted(keys):
                print('{}    {} = "{}"'
                      .format(ind, k, self._operations.get(k, [])))
        print('{}  Children:'.format(ind))
        if not self._children:
            print('{}    -- NONE --'.format(ind))
        else:
            for c in self._children:
                c.dump(indent=indent + 1)

    @property
    def keys(self):
        return self._operations.keys()

    @property
    def visited_keys(self):
        return self._visited_keys

    def get(self, key: str, default=None) -> typing.List[str]:
        self._visited_keys.add(key)
        result = []  # type: typing.List[str]

        for op in self._operations.get(key, []):
            result = op.process(result)
        return result

    def getString(self, key: str, default: str = '') -> str:
        v = self.get(key, default)
        if len(v) == 0:
            return default
        assert len(v) == 1
        return v[0]

    @property
    def TEMPLATE(self) -> str:
        return self.getString('TEMPLATE', 'app')

    def _rawTemplate(self) -> str:
        return self.getString('TEMPLATE')

    @property
    def TARGET(self) -> str:
        return self.getString('TARGET') \
            or os.path.splitext(os.path.basename(self.file))[0]

    @property
    def _INCLUDED(self) -> typing.List[str]:
        return self.get('_INCLUDED', [])


class QmakeParser:
    def __init__(self, *, debug: bool = False) -> None:
        self._Grammar = self._generate_grammar(debug)

    def _generate_grammar(self, debug: bool):
        # Define grammar:
        pp.ParserElement.setDefaultWhitespaceChars(' \t')

        LC = pp.Suppress(pp.Literal('\\') + pp.LineEnd())
        EOL = pp.Suppress(pp.Optional(pp.pythonStyleComment()) + pp.LineEnd())

        Identifier = pp.Word(pp.alphas + '_', bodyChars=pp.alphanums+'_-./')
        Substitution \
            = pp.Combine(pp.Literal('$')
                         + (((pp.Literal('$') + Identifier
                              + pp.Optional(pp.nestedExpr()))
                             | (pp.Literal('(') + Identifier + pp.Literal(')'))
                             | (pp.Literal('{') + Identifier + pp.Literal('}'))
                             | (pp.Literal('$') + pp.Literal('{')
                                + Identifier + pp.Optional(pp.nestedExpr())
                                + pp.Literal('}'))
                             | (pp.Literal('$') + pp.Literal('[') + Identifier
                                + pp.Literal(']'))
                             )))
        # Do not match word ending in '\' since that breaks line
        # continuation:-/
        LiteralValuePart = pp.Word(pp.printables, excludeChars='$#{}()')
        SubstitutionValue \
            = pp.Combine(pp.OneOrMore(Substitution | LiteralValuePart
                                      | pp.Literal('$')))
        Value = (pp.QuotedString(quoteChar='"', escChar='\\')
                 | SubstitutionValue)

        Values = pp.ZeroOrMore(Value)('value')

        Op = pp.Literal('=') | pp.Literal('-=') | pp.Literal('+=') \
            | pp.Literal('*=')

        Operation = Identifier('key') + Op('operation') + Values('value')
        Load = pp.Keyword('load') + pp.Suppress('(') \
            + Identifier('loaded') + pp.Suppress(')')
        Include = pp.Keyword('include') + pp.Suppress('(') \
            + pp.CharsNotIn(':{=}#)\n')('included') + pp.Suppress(')')
        Option = pp.Keyword('option') + pp.Suppress('(') \
            + Identifier('option') + pp.Suppress(')')
        DefineTest = pp.Suppress(pp.Keyword('defineTest')
                                 + pp.Suppress('(') + Identifier
                                 + pp.Suppress(')')
                                 + pp.nestedExpr(opener='{', closer='}')
                                 + pp.LineEnd())  # ignore the whole thing...
        ForLoop = pp.Suppress(pp.Keyword('for') + pp.nestedExpr()
                              + pp.nestedExpr(opener='{', closer='}',
                                              ignoreExpr=None)
                              + pp.LineEnd())  # ignore the whole thing...
        FunctionCall = pp.Suppress(Identifier + pp.nestedExpr())

        Scope = pp.Forward()

        Statement = pp.Group(Load | Include | Option | DefineTest
                             | ForLoop | FunctionCall | Operation)
        StatementLine = Statement + EOL
        StatementGroup = pp.ZeroOrMore(StatementLine | Scope | EOL)

        Block = pp.Suppress('{') + pp.Optional(EOL) \
            + pp.ZeroOrMore(EOL | Statement + EOL | Scope) \
            + pp.Optional(Statement) + pp.Optional(EOL) \
            + pp.Suppress('}') + pp.Optional(EOL)

        Condition = pp.Optional(pp.White()) + pp.CharsNotIn(':{=}#\\\n')
        Condition.setParseAction(lambda x: ' '.join(x).strip())

        SingleLineScope = pp.Suppress(pp.Literal(':')) \
            + pp.Group(Scope | Block | StatementLine)('statements')
        MultiLineScope = Block('statements')

        SingleLineElse = pp.Suppress(pp.Literal(':')) \
            + pp.Group(Scope | StatementLine)('else_statements')
        MultiLineElse = pp.Group(Block)('else_statements')
        Else = pp.Suppress(pp.Keyword('else')) \
            + (SingleLineElse | MultiLineElse)
        Scope <<= pp.Group(Condition('condition')
                           + (SingleLineScope | MultiLineScope)
                           + pp.Optional(Else))

        if debug:
            for ename in 'EOL Identifier Substitution SubstitutionValue ' \
                         'LiteralValuePart Value Values SingleLineScope ' \
                         'MultiLineScope Scope SingleLineElse ' \
                         'MultiLineElse Else Condition Block ' \
                         'StatementGroup Statement Load Include Option ' \
                         'DefineTest ForLoop FunctionCall Operation'.split():
                expr = locals()[ename]
                expr.setName(ename)
                expr.setDebug()

        Grammar = StatementGroup('statements')
        Grammar.ignore(LC)

        return Grammar

    def parseFile(self, file: str):
        print('Parsing \"{}\"...'.format(file))
        try:
            result = self._Grammar.parseFile(file, parseAll=True)
        except pp.ParseException as pe:
            print(pe.line)
            print(' '*(pe.col-1) + '^')
            print(pe)
            raise pe
        return result


def parseProFile(file: str, *, debug=False):
    parser = QmakeParser(debug=debug)
    return parser.parseFile(file)


def map_condition(condition: str) -> str:
    re.sub(r'if\s*\((.*?)\)', r'\1', condition)
    re.sub(r'(^|[^a-zA-Z0-9_])isEmpty\s*\((.*?)\)', r'\2_ISEMPTY', condition)
    re.sub(r'(^|[^a-zA-Z0-9_])contains\s*\((.*?), (.*)?\)',
           r'\2___contains___\3', condition)
    re.sub(r'\s*==\s*', '___STREQUAL___', condition)

    condition = condition.replace('*', '_x_')
    condition = condition.replace('.$$', '__ss_')
    condition = condition.replace('$$', '_ss_')

    condition = condition.replace('!', 'NOT ')
    condition = condition.replace('&&', ' AND ')
    condition = condition.replace('|', ' OR ')

    cmake_condition = ''
    for part in condition.split():
        # some features contain e.g. linux, that should not be
        # turned upper case
        feature = re.match(r"(qtConfig|qtHaveModule)\(([a-zA-Z0-9_-]+)\)",
                           part)
        if feature:
            if (feature.group(1) == "qtHaveModule"):
                part = 'TARGET {}'.format(map_qt_base_library(
                                            feature.group(2)))
            else:
                feature = featureName(feature.group(2))
                if feature.startswith('system_') and substitute_libs(feature[7:]) != feature[7:]:
                    # Qt6 always uses system libraries!
                    part = 'ON'
                else:
                    part = 'QT_FEATURE_' + feature
        else:
            part = substitute_platform(part)

        part = part.replace('true', 'ON')
        part = part.replace('false', 'OFF')
        cmake_condition += ' ' + part
    return cmake_condition.strip()


def handle_subdir(scope: Scope, cm_fh: typing.IO[str], *,
                  indent: int = 0) -> None:
    assert scope.TEMPLATE == 'subdirs'
    ind = '    ' * indent
    for sd in scope.get('SUBDIRS', []):
        full_sd = os.path.join(scope.basedir, sd)
        if os.path.isdir(full_sd):
            cm_fh.write('{}add_subdirectory({})\n'.format(ind, sd))
        elif os.path.isfile(full_sd):
            subdir_result = parseProFile(full_sd, debug=False)
            subdir_scope \
                = Scope.FromDict(scope, full_sd,
                                 subdir_result.asDict().get('statements'),
                                 '', scope.basedir)

            cmakeify_scope(subdir_scope, cm_fh, indent=indent + 1)
        elif sd.startswith('-'):
            cm_fh.write('{}### remove_subdirectory'
                        '("{}")\n'.format(ind, sd[1:]))
        else:
            print('    XXXX: SUBDIR {} in {}: Not found.'.format(sd, scope))

    for c in scope.children():
        cond = c.condition
        if cond == 'else':
            cm_fh.write('\n{}else()\n'.format(ind))
        elif cond:
            cm_fh.write('\n{}if({})\n'.format(ind, cond))

        handle_subdir(c, cm_fh, indent=indent + 1)

        if cond:
            cm_fh.write('{}endif()\n'.format(ind))


def sort_sources(sources) -> typing.List[str]:
    to_sort = {}  # type: typing.Dict[str, typing.List[str]]
    for s in sources:
        if s is None:
            continue

        dir = os.path.dirname(s)
        base = os.path.splitext(os.path.basename(s))[0]
        if base.endswith('_p'):
            base = base[:-2]
        sort_name = os.path.join(dir, base)

        array = to_sort.get(sort_name, [])
        array.append(s)

        to_sort[sort_name] = array

    lines = []
    for k in sorted(to_sort.keys()):
        lines.append(' '.join(sorted(to_sort[k])))

    return lines


def write_header(cm_fh: typing.IO[str], name: str,
                 typename: str, *, indent: int = 0):
    cm_fh.write('{}###########################################'
                '##########################\n'.format(spaces(indent)))
    cm_fh.write('{}## {} {}:\n'.format(spaces(indent), name, typename))
    cm_fh.write('{}###########################################'
                '##########################\n\n'.format(spaces(indent)))


def write_scope_header(cm_fh: typing.IO[str], *, indent: int = 0):
    cm_fh.write('\n{}## Scopes:\n'.format(spaces(indent)))
    cm_fh.write('{}###########################################'
                '##########################\n'.format(spaces(indent)))


def write_sources_section(cm_fh: typing.IO[str], scope: Scope, *,
                          indent: int = 0, known_libraries=set()) \
        -> typing.Set[str]:
    ind = spaces(indent)
    scope.reset_visited_keys()

    plugin_type = scope.get('PLUGIN_TYPE')

    if plugin_type:
        cm_fh.write('{}    TYPE {}\n'.format(ind, plugin_type[0]))

    sources = scope.get('SOURCES') + scope.get('HEADERS') \
        + scope.get('OBJECTIVE_SOURCES') + scope.get('NO_PCH_SOURCES') \
        + scope.get('FORMS')
    resources = scope.get('RESOURCES')
    if resources:
        qrc_only = True
        for r in resources:
            if not r.endswith('.qrc'):
                qrc_only = False
                break

        if not qrc_only:
            print('     XXXX Ignoring non-QRC file resources.')
        else:
            sources += resources

    vpath = scope.get('VPATH')

    sources = [map_source_to_cmake(s, scope.basedir, vpath) for s in sources]
    if sources:
        cm_fh.write('{}    SOURCES\n'.format(ind))
    for l in sort_sources(sources):
        cm_fh.write('{}        {}\n'.format(ind, l))

    defines = scope.get('DEFINES')
    if defines:
        cm_fh.write('{}    DEFINES\n'.format(ind))
        for d in defines:
            d = d.replace('=\\\\\\"$$PWD/\\\\\\"',
                          '="${CMAKE_CURRENT_SOURCE_DIR}/"')
            cm_fh.write('{}        {}\n'.format(ind, d))
    includes = scope.get('INCLUDEPATH')
    if includes:
        cm_fh.write('{}    INCLUDE_DIRECTORIES\n'.format(ind))
        for i in includes:
            i = i.rstrip('/') or ('/')
            cm_fh.write('{}        {}\n'.format(ind, i))

    dependencies = [map_qt_library(q) for q in scope.get('QT')
                    if map_qt_library(q) not in known_libraries]
    dependencies += [map_qt_library(q) for q in scope.get('QT_FOR_PRIVATE')
                     if map_qt_library(q) not in known_libraries]
    dependencies += scope.get('QMAKE_USE_PRIVATE') \
        + scope.get('LIBS_PRIVATE') + scope.get('LIBS')
    if dependencies:
        cm_fh.write('{}    LIBRARIES\n'.format(ind))
        is_framework = False
        for d in dependencies:
            if d == '-framework':
                is_framework = True
                continue
            if is_framework:
                d = '${FW%s}' % d
            if d.startswith('-l'):
                d = d[2:]

            if d.startswith('-'):
                d = '# Remove: {}'.format(d[1:])
            else:
                d = substitute_libs(d)
            cm_fh.write('{}        {}\n'.format(ind, d))
            is_framework = False

    return set(scope.keys) - scope.visited_keys


def is_simple_condition(condition: str) -> bool:
    return ' ' not in condition \
        or (condition.startswith('NOT ') and ' ' not in condition[4:])


def write_ignored_keys(scope: Scope, ignored_keys, indent) -> str:
    result = ''
    for k in sorted(ignored_keys):
        if k == '_INCLUDED' or k == 'TARGET' or k == 'QMAKE_DOCS':
            # All these keys are actually reported already
            continue
        values = scope.get(k)
        value_string = '<EMPTY>' if not values \
            else '"' + '" "'.join(scope.get(k)) + '"'
        result += '{}# {} = {}\n'.format(indent, k, value_string)
    return result


def _iterate_expr_tree(expr, op, matches):
    assert expr.func == op
    keepers = ()
    for arg in expr.args:
        if arg in matches:
            matches = tuple(x for x in matches if x != arg)
        elif arg == op:
            (matches, extra_keepers) = _iterate_expr_tree(arg, op, matches)
            keepers = (*keepers, *extra_keepers)
        else:
            keepers = (*keepers, arg)
    return (matches, keepers)


def _simplify_expressions(expr, op, matches, replacement):
    args = expr.args
    for arg in args:
        expr = expr.subs(arg, _simplify_expressions(arg, op, matches,
                                                    replacement))

    if expr.func == op:
        (to_match, keepers) = tuple(_iterate_expr_tree(expr, op, matches))
        if len(to_match) == 0:
            # build expression with keepers and replacement:
            if keepers:
                start = replacement
                current_expr = None
                last_expr = keepers[-1]
                for repl_arg in keepers[:-1]:
                    current_expr = op(start, repl_arg)
                    start = current_expr
                top_expr = op(start, last_expr)
            else:
                top_expr = replacement

            expr = expr.subs(expr, top_expr)

    return expr


def _simplify_flavors_in_condition(base: str, flavors, expr):
    ''' Simplify conditions based on the knownledge of which flavors
        belong to which OS. '''
    base_expr = simplify_logic(base)
    false_expr = simplify_logic('false')
    for flavor in flavors:
        flavor_expr = simplify_logic(flavor)
        expr = _simplify_expressions(expr, And, (base_expr, flavor_expr,),
                                     flavor_expr)
        expr = _simplify_expressions(expr, Or, (base_expr, flavor_expr),
                                     base_expr)
        expr = _simplify_expressions(expr, And, (Not(base_expr), flavor_expr,),
                                     false_expr)
    return expr


def _recursive_simplify(expr):
    ''' Simplify the expression as much as possible based on
        domain knowledge. '''
    input_expr = expr

    # Simplify even further, based on domain knowledge:
    apples = ('APPLE_OSX', 'APPLE_UIKIT', 'APPLE_IOS',
              'APPLE_TVOS', 'APPLE_WATCHOS',)
    bsds = ('APPLE', 'FREEBSD', 'OPENBSD', 'NETBSD',)
    unixes = ('APPLE', *apples, 'BSD', *bsds, 'LINUX',
              'ANDROID', 'ANDROID_EMBEDDED',
              'INTEGRITY', 'VXWORKS', 'QNX', 'WASM')

    unix_expr = simplify_logic('UNIX')
    win_expr = simplify_logic('WIN32')
    false_expr = simplify_logic('false')
    true_expr = simplify_logic('true')

    expr = expr.subs(Not(unix_expr), win_expr)  # NOT UNIX -> WIN32
    expr = expr.subs(Not(win_expr), unix_expr)  # NOT WIN32 -> UNIX

    # UNIX [OR foo ]OR WIN32 -> ON [OR foo]
    expr = _simplify_expressions(expr, Or, (unix_expr, win_expr,), true_expr)
    # UNIX  [AND foo ]AND WIN32 -> OFF [AND foo]
    expr = _simplify_expressions(expr, And, (unix_expr, win_expr,), false_expr)
    for unix_flavor in unixes:
        #  unix_flavor [AND foo ] AND WIN32 -> FALSE [AND foo]
        flavor_expr = simplify_logic(unix_flavor)
        expr = _simplify_expressions(expr, And, (win_expr, flavor_expr,),
                                     false_expr)

    expr = _simplify_flavors_in_condition('WIN32', ('WINRT',), expr)
    expr = _simplify_flavors_in_condition('APPLE', apples, expr)
    expr = _simplify_flavors_in_condition('BSD', bsds, expr)
    expr = _simplify_flavors_in_condition('UNIX', unixes, expr)

    # Now simplify further:
    expr = simplify_logic(expr)

    while expr != input_expr:
        input_expr = expr
        expr = _recursive_simplify(expr)

    return expr


def simplify_condition(condition: str) -> str:
    input_condition = condition.strip()

    # Map to sympy syntax:
    condition = ' ' + input_condition + ' '
    condition = condition.replace('(', ' ( ')
    condition = condition.replace(')', ' ) ')

    tmp = ''
    while tmp != condition:
        tmp = condition

        condition = condition.replace(' NOT ', ' ~ ')
        condition = condition.replace(' AND ', ' & ')
        condition = condition.replace(' OR ', ' | ')
        condition = condition.replace(' ON ', 'true')
        condition = condition.replace(' OFF ', 'false')

    try:
        # Generate and simplify condition using sympy:
        condition_expr = simplify_logic(condition)
        condition = str(_recursive_simplify(condition_expr))

        # Map back to CMake syntax:
        condition = condition.replace('~', 'NOT ')
        condition = condition.replace('&', 'AND')
        condition = condition.replace('|', 'OR')
        condition = condition.replace('True', 'ON')
        condition = condition.replace('False', 'OFF')
    except:
        # sympy did not like our input, so leave this condition alone:
        condition = input_condition

    if condition == '':
        condition = 'ON'
    return condition


def recursive_evaluate_scope(scope: Scope, parent_condition: str = '',
                             previous_condition: str = '') -> str:
    current_condition = scope.condition
    total_condition = current_condition
    if total_condition == 'else':
        assert previous_condition, \
            "Else branch without previous condition in: %s" % scope.file
        if previous_condition.startswith('NOT '):
            total_condition = previous_condition[4:]
        elif is_simple_condition(previous_condition):
            total_condition = 'NOT {}'.format(previous_condition)
        else:
            total_condition = 'NOT ({})'.format(previous_condition)
    if parent_condition:
        if not total_condition:
            total_condition = parent_condition
        else:
            if is_simple_condition(parent_condition) \
                    and is_simple_condition(total_condition):
                total_condition = '{} AND {}'.format(parent_condition,
                                                     total_condition)
            elif is_simple_condition(total_condition):
                total_condition = '({}) AND {}'.format(parent_condition,
                                                       total_condition)
            elif is_simple_condition(parent_condition):
                total_condition = '{} AND ({})'.format(parent_condition,
                                                       total_condition)
            else:
                total_condition = '({}) AND ({})'.format(parent_condition,
                                                         total_condition)

    scope.total_condition = simplify_condition(total_condition)

    prev_condition = ''
    for c in scope.children:
        prev_condition = recursive_evaluate_scope(c, total_condition,
                                                  prev_condition)

    return current_condition


def write_extend_target(cm_fh: typing.IO[str], target: str,
                        scope: Scope, indent: int = 0):
    extend_qt_io_string = io.StringIO()
    ignored_keys = write_sources_section(extend_qt_io_string, scope)
    extend_qt_string = extend_qt_io_string.getvalue()

    ignored_keys_report = write_ignored_keys(scope, ignored_keys,
                                             spaces(indent + 1))
    if extend_qt_string and ignored_keys_report:
        ignored_keys_report = '\n' + ignored_keys_report

    extend_scope = '\n{}extend_target({} CONDITION {}\n' \
                   '{}{})\n'.format(spaces(indent), target,
                                    scope.total_condition,
                                    extend_qt_string, ignored_keys_report)

    if not extend_qt_string:
        if ignored_keys_report:
            # Comment out the generated extend_target call because there
            # no sources were found, but keep it commented for
            # informational purposes.
            extend_scope = ''.join(['#' + line for line in
                                    extend_scope.splitlines(keepends=True)])
        else:
            extend_scope = ''  # Nothing to report, so don't!

    cm_fh.write(extend_scope)


def flatten_scopes(scope: Scope) -> typing.List[Scope]:
    result = [scope]  # type: typing.List[Scope]
    for c in scope.children:
        result += flatten_scopes(c)
    return result


def merge_scopes(scopes: typing.List[Scope]) -> typing.List[Scope]:
    result = []  # type: typing.List[Scope]

    # Merge scopes with their parents:
    known_scopes = {}  # type: typing.Mapping[str, Scope]
    for scope in scopes:
        total_condition = scope.total_condition
        if total_condition == 'OFF':
            # ignore this scope entirely!
            pass
        elif total_condition in known_scopes:
            known_scopes[total_condition].merge(scope)
        else:
            # Keep everything else:
            result.append(scope)
            known_scopes[total_condition] = scope

    return result


def write_main_part(cm_fh: typing.IO[str], name: str, typename: str,
                    cmake_function: str, scope: Scope, *,
                    extra_lines: typing.List[str] = [],
                    indent: int = 0,
                    **kwargs: typing.Any):
    # Evaluate total condition of all scopes:
    recursive_evaluate_scope(scope)

    # Get a flat list of all scopes but the main one:
    scopes = flatten_scopes(scope)
    total_scopes = len(scopes)
    # Merge scopes based on their conditions:
    scopes = merge_scopes(scopes)
    print("xxxxxx {} scopes, {} after merging!".format(total_scopes, len(scopes)))

    assert len(scopes)
    assert scopes[0].total_condition == 'ON'

    # Now write out the scopes:
    write_header(cm_fh, name, typename, indent=indent)

    cm_fh.write('{}{}({}\n'.format(spaces(indent), cmake_function, name))
    for extra_line in extra_lines:
        cm_fh.write('{}    {}\n'.format(spaces(indent), extra_line))

    ignored_keys = write_sources_section(cm_fh, scopes[0], indent=indent, **kwargs)
    ignored_keys_report = write_ignored_keys(scopes[0], ignored_keys,
                                             spaces(indent + 1))
    if ignored_keys_report:
        cm_fh.write(ignored_keys_report)

    # Footer:
    cm_fh.write('{})\n'.format(spaces(indent)))

    # Scopes:
    if len(scopes) == 1:
        return

    write_scope_header(cm_fh, indent=indent)

    for c in scopes[1:]:
        write_extend_target(cm_fh, name, c, indent=indent)


def write_module(cm_fh: typing.IO[str], scope: Scope, *,
                 indent: int = 0) -> None:
    module_name = scope.TARGET
    assert module_name.startswith('Qt')

    extra = []
    if 'static' in scope.get('CONFIG'):
        extra.append('STATIC')
    if 'no_module_headers' in scope.get('CONFIG'):
        extra.append('NO_MODULE_HEADERS')

    write_main_part(cm_fh, module_name[2:], 'Module', 'add_qt_module', scope,
                    extra_lines=extra, indent=indent,
                    known_libraries={'Qt::Core', })

    if 'qt_tracepoints' in scope.get('CONFIG'):
        tracepoints = map_to_file(scope.getString('TRACEPOINT_PROVIDER'),
                                  scope.basedir, scope.currentdir)
        cm_fh.write('\n\n{}qt_create_tracepoints({} {})\n'
                    .format(spaces(indent), module_name[2:], tracepoints))


def write_tool(cm_fh: typing.IO[str], scope: Scope, *,
               indent: int = 0) -> None:
    tool_name = scope.TARGET

    write_main_part(cm_fh, tool_name, 'Tool', 'add_qt_tool', scope,
                    indent=indent, known_libraries={'Qt::Core', })


def write_test(cm_fh: typing.IO[str], scope: Scope, *,
               indent: int = 0) -> None:
    test_name = scope.TARGET
    assert test_name

    write_main_part(cm_fh, test_name, 'Test', 'add_qt_test', scope,
                    indent=indent, known_libraries={'Qt::Core', 'Qt::Test', })


def write_binary(cm_fh: typing.IO[str], scope: Scope,
                 gui: bool = False, *, indent: int = 0) -> None:
    binary_name = scope.TARGET
    assert binary_name

    extra = ['GUI', ] if gui else []
    write_main_part(cm_fh, binary_name, 'Binary', 'add_qt_executable', scope,
                    extra_lines=extra, indent=indent,
                    known_libraries={'Qt::Core', })


def write_plugin(cm_fh, scope, *, indent: int = 0):
    plugin_name = scope.TARGET
    assert plugin_name

    write_main_part(cm_fh, plugin_name, 'Plugin', 'add_qt_plugin', scope,
                    indent=indent, known_libraries={'QtCore', })


def handle_app_or_lib(scope: Scope, cm_fh: typing.IO[str], *,
                      indent: int = 0) -> None:
    assert scope.TEMPLATE in ('app', 'lib')

    is_lib = scope.TEMPLATE == 'lib'
    is_plugin = any('qt_plugin' == s for s in scope.get('_LOADED', []))

    if is_lib or 'qt_module' in scope.get('_LOADED', []):
        write_module(cm_fh, scope, indent=indent)
    elif is_plugin:
        write_plugin(cm_fh, scope, indent=indent)
    elif 'qt_tool' in scope.get('_LOADED', []):
        write_tool(cm_fh, scope, indent=indent)
    else:
        if 'testcase' in scope.get('CONFIG') \
                or 'testlib' in scope.get('CONFIG'):
            write_test(cm_fh, scope, indent=indent)
        else:
            gui = 'console' not in scope.get('CONFIG')
            write_binary(cm_fh, scope, gui, indent=indent)

    docs = scope.getString("QMAKE_DOCS")
    if docs:
        cm_fh.write("\n{}add_qt_docs({})\n"
                    .format(spaces(indent),
                            map_to_file(docs, scope.basedir,
                                        scope.currentdir)))


def cmakeify_scope(scope: Scope, cm_fh: typing.IO[str], *,
                   indent: int = 0) -> None:
    template = scope.TEMPLATE
    if template == 'subdirs':
        handle_subdir(scope, cm_fh, indent=indent)
    elif template in ('app', 'lib'):
        handle_app_or_lib(scope, cm_fh, indent=indent)
    else:
        print('    XXXX: {}: Template type {} not yet supported.'
              .format(scope.file, template))


def generate_cmakelists(scope: Scope) -> None:
    with open(scope.cMakeListsFile, 'w') as cm_fh:
        assert scope.file
        cm_fh.write('# Generated from {}.\n\n'
                    .format(os.path.basename(scope.file)))
        cmakeify_scope(scope, cm_fh)


def do_include(scope: Scope, *, debug: bool = False) -> None:
    for c in scope.children:
        do_include(c)

    for i in scope._INCLUDED:
        dir = scope.basedir
        include_file = i
        if not include_file:
            continue
        if not os.path.isfile(include_file):
            print('    XXXX: Failed to include {}.'.format(include_file))
            continue

        include_result = parseProFile(include_file, debug=debug)
        include_scope \
            = Scope.FromDict(None, include_file,
                             include_result.asDict().get('statements'),
                             '', dir)  # This scope will be merged into scope!

        do_include(include_scope)

        scope.merge(include_scope)


def main() -> None:
    args = _parse_commandline()

    debug_parsing = args.debug_parser or args.debug

    for file in args.files:
        parseresult = parseProFile(file, debug=debug_parsing)

        if args.debug_parse_result or args.debug:
            print('\n\n#### Parser result:')
            print(parseresult)
            print('\n#### End of parser result.\n')
        if args.debug_parse_dictionary or args.debug:
            print('\n\n####Parser result dictionary:')
            print(parseresult.asDict())
            print('\n#### End of parser result dictionary.\n')

        file_scope = Scope.FromDict(None, file,
                                    parseresult.asDict().get('statements'))

        if args.debug_pro_structure or args.debug:
            print('\n\n#### .pro/.pri file structure:')
            print(file_scope.dump())
            print('\n#### End of .pro/.pri file structure.\n')

        do_include(file_scope, debug=debug_parsing)

        if args.debug_full_pro_structure or args.debug:
            print('\n\n#### Full .pro/.pri file structure:')
            print(file_scope.dump())
            print('\n#### End of full .pro/.pri file structure.\n')

        generate_cmakelists(file_scope)


if __name__ == '__main__':
    main()
