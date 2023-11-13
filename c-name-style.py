import re
import sys
from argparse import ArgumentParser
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Callable

from clang.cindex import Config, Cursor, CursorKind, Index, LinkageKind, TokenKind, TypeKind, Token, TranslationUnit, conf  # type: ignore


class SubstTemplate(Template):
    braceidpattern = r"(?a:[_a-z][_a-z0-9\-:]*)"


@dataclass
class Rule:
    name: str
    kinds: list[str] | None
    visibility: list[str] | None
    types: list[str] | None
    pointer: int | bool | None
    parent_match: str | None
    prefix: str | None
    suffix: str | None
    rule: str | None
    allow_rule: str | None


class RuleSet:
    def __init__(self, config: ConfigParser) -> None:
        self.rules: list[Rule] = []
        self.placeholders: dict[str, str] = {}

        for section_name in config.sections():
            section = config[section_name]

            if section_name == "placeholders":
                self.placeholders = {f"p:{k}": v for k, v in config.items(section_name)}
                continue

            kinds = section.get("kind")
            if kinds is not None:
                kinds = [x.strip() for x in kinds.split(",")]

            variable_types = section.get("type")
            if variable_types is not None:
                variable_types = [x.strip() for x in variable_types.split(",")]

            visibility = section.get("visibility")
            if visibility is not None:
                visibility = [x.strip() for x in visibility.split(",")]

            # getboolean parses '1' as true
            try:
                pointer = section.getint("pointer")
            except ValueError:
                pointer = section.getboolean("pointer")

            parent_match = section.get("parent_match")
            prefix = section.get("prefix")
            suffix = section.get("suffix")

            rule = section.get("rule")
            allow_rule = section.get("allow-rule")
            # It's OK for there to be no rule/allow-rule if there's a prefix or suffix
            if rule is None and allow_rule is None and prefix is None and suffix is None:
                raise Exception(f"Section '{section_name}' does not have a 'rule' or 'allow-rule' member")
            if rule is not None and allow_rule is not None:
                raise Exception(f"Section '{section_name}' may not have both a 'rule' and an 'allow-rule")

            self.rules.append(
                Rule(
                    name=section_name,
                    kinds=kinds,
                    visibility=visibility,
                    types=variable_types,
                    pointer=pointer,
                    parent_match=parent_match,
                    prefix=prefix,
                    suffix=suffix,
                    rule=rule,
                    allow_rule=allow_rule
                )
            )

@dataclass
class IgnoreComment:
    token: Token
    used: bool = False

class Processor:
    _KIND_EXPANSION = {
        "tag": ["struct_tag", "enum_tag", "union_tag"],
        "typedef": ["struct_typedef", "enum_typedef", "union_typedef", "function_typedef", "scalar_typedef"],
        "member": ["struct_member", "union_member"],
    }

    _COMMENT_REGEX = r"(?://\s*c-name-style\s+(.*))|(?:/\*\s*c-name-style\s+(.*)\*/)"

    def __init__(self, rule_set: RuleSet, verbosity: int) -> None:
        self._rule_set = rule_set
        self._verbosity = verbosity

        self._ignore_comments: dict[str, IgnoreComment] = {} # filename:line -> IgnoreComment
        self._has_failures = False

    def _sub_placeholders(self, template: str, placeholders: dict[str, str]) -> str:
        # Do the placeholders section first
        result = template
        if len(self._rule_set.placeholders) > 0:
            result = SubstTemplate(result).safe_substitute(self._rule_set.placeholders)
        result = SubstTemplate(result).safe_substitute(placeholders)
        return result

    def _is_struct_enum_union_unnamed(self, cursor: Cursor) -> bool:
        # If a struct/enum is unnamed, clang takes the typedef name as the name.
        # (The C API has methods to query this, but they're not exposed to Python)
        # Therefore we need to look at the tokens to figure out.
        # Look for the 'struct', then the following '{', and see if the typedef name appears in between.
        # (People can do things like 'typedef struct /* foo */ {')
        # We might also see e.g. 'typedef struct T_tag T_t', so there might not be a '{'
        # Look for 'struct/enum' and '{', with the thing that might be the tag name or might be the
        # typedef name in the middle. If we find the 'struct/enum' and '{' but not the name, it's
        # unnamed.
        if cursor.kind == CursorKind.ENUM_DECL:
            t = "enum"
        elif cursor.kind == CursorKind.STRUCT_DECL:
            t = "struct"
        elif cursor.kind == CursorKind.UNION_DECL:
            t = "union"
        else:
            raise AssertionError()
        tokens = [x.spelling for x in cursor.get_tokens()]
        try:
            type_pos = tokens.index(t)
            open_brace_pos = tokens.index("{", type_pos)
        except ValueError:
            return False
        try:
            _dummy = tokens.index(cursor.spelling, type_pos, open_brace_pos)
            return False
        except ValueError:
            return True

    # (type, visibility)
    def _get_config_kind(self, cursor: Cursor, file_path: Path) -> tuple[str | None, str | None]:
        is_header = file_path.suffix in [".h", ".hpp"]
        global_or_file = "global" if is_header else "file"
        if cursor.kind == CursorKind.PARM_DECL:
            return ("parameter", None)
        if cursor.kind == CursorKind.VAR_DECL:
            # In header files, all variables are global
            if is_header:
                return ("variable", "global")
            if cursor.linkage == LinkageKind.INTERNAL:
                return ("variable", "file")
            if cursor.linkage == LinkageKind.NO_LINKAGE:
                return ("variable", "local")
            if cursor.linkage == LinkageKind.EXTERNAL:
                # Both 'int Foo' and 'extern int foo' come up here. We want to exclude 'extern' as people don't have control
                # over those names. People can't control the names of symbols defined elsewhere
                if conf.lib.clang_Cursor_hasVarDeclExternalStorage(cursor):
                    return (None, None)
                return ("variable", "global")
            print(f"WARNING: Unexpected linkage {cursor.linkage} for {cursor.spelling}")
            return (None, None)
        if cursor.kind == CursorKind.FUNCTION_DECL:
            # Inline functions in headers are counted as globals
            if cursor.linkage == LinkageKind.EXTERNAL or (
                conf.lib.clang_Cursor_isFunctionInlined(cursor) and is_header
            ):
                return ("function", "global")
            if cursor.linkage == LinkageKind.INTERNAL:
                return ("function", "file")
            print(f"WARNING: Unexpected linkage {cursor.linkage} for {cursor.spelling}")
            return (None, None)
        if cursor.kind == CursorKind.STRUCT_DECL:
            if self._is_struct_enum_union_unnamed(cursor):
                return (None, None)
            return ("struct_tag", global_or_file)
        if cursor.kind == CursorKind.UNION_DECL:
            if self._is_struct_enum_union_unnamed(cursor):
                return (None, None)
            return ("union_tag", global_or_file)
        if cursor.kind == CursorKind.ENUM_DECL:
            if self._is_struct_enum_union_unnamed(cursor):
                return (None, None)
            return ("enum_tag", global_or_file)
        if cursor.kind == CursorKind.TYPEDEF_DECL:
            underlying_type = cursor.underlying_typedef_type.get_canonical()
            # Unwrap any pointers
            while underlying_type.kind == TypeKind.POINTER:
                underlying_type = underlying_type.get_pointee()
            if underlying_type.kind == TypeKind.RECORD:
                # I don't think cindex exposes a way to tell the difference...
                if underlying_type.spelling.startswith("union "):
                    return ("union_typedef", global_or_file)
                return ("struct_typedef", global_or_file)
            if underlying_type.kind == TypeKind.ENUM:
                return ("enum_typedef", global_or_file)
            if underlying_type.kind == TypeKind.FUNCTIONPROTO:
                return ("function_typedef", global_or_file)
            return ("scalar_typedef", global_or_file)
        if cursor.kind == CursorKind.FIELD_DECL:
            # I don't think cindex exposes a way to tell the difference...
            if cursor.semantic_parent.type.spelling.startswith("union "):
                return ("union_member", global_or_file)
            return ("struct_member", None)
        if cursor.kind == CursorKind.ENUM_CONSTANT_DECL:
            return ("enum_constant", global_or_file)
        return (None, None)
    
    def _rule_applies(self, cursor: Cursor, rule: Rule, config_kind: str, visibility: str, pointer_level: int) -> bool:
        rule_kinds = rule.kinds
        if rule_kinds is not None:
            for rule_kind in rule_kinds:
                if rule_kind in Processor._KIND_EXPANSION:
                    rule_kinds.extend(Processor._KIND_EXPANSION[rule_kind])
                    rule_kinds.remove(rule_kind)
            if config_kind not in rule_kinds:
                if self._verbosity > 2:
                    print(f"  Skip rule '{rule.name}': kind '{config_kind}' not in '{', '.join(rule.kinds)}'")
                return False

        if (
            pointer_level is not None
            and rule.pointer is not None
            and not (
                (isinstance(rule.pointer, bool) and rule.pointer == (pointer_level > 0))
                or (rule.pointer == pointer_level)
            )
        ):
            if self._verbosity > 2:
                print(f"  Skip rule '{rule.name}': pointer level '{pointer_level}' does not match '{rule.pointer}'")
            return False

        if rule.types is not None and not any(re.fullmatch(x, cursor.type.spelling) for x in rule.types):
            if self._verbosity > 2:
                print(f"  Skip rule '{rule.name}': type '{cursor.type.spelling}' not in '{', '.join(rule.types)}'")
            return False

        if visibility is not None and rule.visibility is not None and visibility not in rule.visibility:
            if self._verbosity > 2:
                print(f"  Skip rule '{rule.name}': visibility '{visibility}' not in '{', '.join(rule.visibility)}'")
            return False

        if (
            rule.parent_match is not None
            and cursor.kind == CursorKind.ENUM_CONSTANT_DECL
            and cursor.semantic_parent.is_anonymous()
        ):
            if self._verbosity > 2:
                print(f"  Skip rule '{rule.name}: parent_match specified but enum is anonymous")
            return False
        
        return True
    
    # return: true -> everything is OK, false -> rule failed, None -> continue processing
    def _test_rule(self, cursor: Cursor, rule: Rule, prefix_rules: list[Rule], suffix_rules: list[Rule], location: str, substitute_vars: dict[str, str]) -> bool | None:
        ignore_key = f"{cursor.location.file.name}:{cursor.location.line}"
        ignore_comment = self._ignore_comments.get(ignore_key)
        name = cursor.spelling
        name_without_prefix_suffix = name
        success = True

        def test_affix_rules(affix_rules: list[Rule], is_prefix: bool) -> str:
            nonlocal name_without_prefix_suffix
            nonlocal success 
            expanded_affix = None

            if len(affix_rules) > 0:
                accessor = (lambda x: x.prefix) if is_prefix else (lambda x: x.suffix)
                term = "prefix" if is_prefix else "suffix"
                expanded_affix = "".join(self._sub_placeholders(accessor(x), substitute_vars) for x in affix_rules)  # type: ignore
                regex = "^" + expanded_affix if is_prefix else expanded_affix + "$"
                match = re.search(regex, name_without_prefix_suffix)
                if match is None:
                    if ignore_comment is not None:
                        ignore_comment.used = True
                        if self._verbosity > 1:
                            print(
                                f"    Ignored by comment: Name '{name}' is missing {term} '{expanded_affix}' from [{', '.join(x.name for x in affix_rules)}]"
                            ) 
                    else:
                        print(
                            f"{location} - Name '{name}' is missing {term} '{expanded_affix}' from [{', '.join(x.name for x in affix_rules)}]"
                        )
                        success = False
                else:
                    name_without_prefix_suffix = name_without_prefix_suffix[match.end() :] if is_prefix else name_without_prefix_suffix[: match.start()]

            return expanded_affix
        
        # If the affix is an empty string, then the accumulated affix doesn't apply to this rule
        expanded_prefix = test_affix_rules(prefix_rules, is_prefix=True) if rule.prefix != "" else None
        expanded_suffix = test_affix_rules(suffix_rules, is_prefix=False) if rule.suffix != "" else None

        if cursor.kind == CursorKind.ENUM_CONSTANT_DECL:
            parent_name = cursor.semantic_parent.spelling
            if rule.parent_match is not None:
                # We checked earlier that the enum isn't anonymous if the rule has parent_match
                assert not cursor.semantic_parent.is_anonymous()
                match = re.fullmatch(rule.parent_match, parent_name)
                if match is None:
                    print(
                        f"{location} - WARNING: Rule '{rule.name}' parent_match '{rule.parent_match}' does not match parent '{parent_name}'"
                    )
                else:
                    try:
                        parent_name = match.group("name")
                    except IndexError:
                        print(
                            f"WARNING: Rule '{rule.name}' parent_match '{rule.rule}' does not have a capture group called 'name'"
                        )
            substitute_vars["parent"] = re.escape(parent_name)
            substitute_vars["parent:upper-snake"] = re.escape(re.sub(r"(?<!^)(?=[A-Z])", "_", parent_name).upper())

        rule_text = rule.rule or rule.allow_rule
        assert rule_text is not None
        rule_regex = self._sub_placeholders(rule_text or rule.allow_rule, substitute_vars)
        rule_name = f"'{rule.name}' ('{rule_regex}'"
        parts = []
        if expanded_prefix is not None:
            parts.append(f"prefix '{expanded_prefix}'")
        if expanded_suffix is not None:
            parts.append(f"suffix '{expanded_suffix}'")
        if len(parts) > 0:
            rule_name += " with " + ", ".join(parts)
        rule_name += ")"

        if self._verbosity > 1:
            print(
                f"  Testing rule {rule_name}. Rule: '{rule_text}' (expanded: '{rule_regex}'); without prefix/suffixes: '{name_without_prefix_suffix}'; placeholders:"
            )
            for k, v in substitute_vars.items():
                print(f"    - {k}: {v}")
        if re.fullmatch(rule_regex, name_without_prefix_suffix) is None:
            if ignore_comment is not None:
                ignore_comment.used = True
                if self._verbosity > 1:
                    print(f"    Ignored by comment: '{name}' fails rule {rule_name} but was ignored by a comment")
            # rule: return true or false. allow_rule: return true or None
            elif rule.rule is not None:
                print(f"{location} - Name '{name}' fails rule {rule_name}")
                success = False
            else:
                assert rule.allow_rule is not None
                if self._verbosity > 1:
                    print(f"{location} - Name '{name}' fails allow-rule {rule_name}. Continuing...")
                success = None
        elif self._verbosity > 1:
            print(f"    Name '{name}' allowed by rule '{rule.name}'")

        return success

    def _process_node(self, cursor: Cursor) -> bool:
        # We look for ignores 

        if not conf.lib.clang_Location_isFromMainFile(cursor.location):
            return True

        file_path = Path(cursor.location.file.name)

        config_kind, visibility = self._get_config_kind(cursor, file_path)

        if config_kind is None:
            return True
        
        location = f"{cursor.location.file}:{cursor.location.line}:{cursor.location.column}"
        name = cursor.spelling

        pointer_level = None
        if cursor.kind in [CursorKind.VAR_DECL, CursorKind.PARM_DECL, CursorKind.TYPEDEF_DECL, CursorKind.FIELD_DECL]:
            pointer_level = 0
            # If it's a typedef, qualify it as 'pointer' if it typedef's a pointer
            pointer_type = (
                cursor.underlying_typedef_type.get_canonical()
                if cursor.kind == CursorKind.TYPEDEF_DECL
                else cursor.type
            )
            while pointer_type.kind == TypeKind.POINTER:
                pointer_level += 1
                pointer_type = pointer_type.get_pointee()

        substitute_vars = {
            "filename": re.escape(file_path.stem),
            "case:camel": "[a-z][a-zA-Z0-9]*",
            "case:pascal": "[A-Z][a-zA-Z0-9]*",
            "case:snake": "[a-z]([a-z0-9_]*[a-z0-9])?",
            "case:upper-snake": "[A-Z]([A-Z0-9_]*[A-Z0-9])?",
            "pointer-level": str(pointer_level),
        }

        if self._verbosity > 0:
            print(
                f"{location} - Name: '{name}'; kind: {config_kind}; visibility: {visibility}; "
                + f"pointer: {pointer_level}; type: '{cursor.type.spelling}'"
            )

        prefix_rules: list[Rule] = []
        suffix_rules: list[Rule] = []
        for rule in self._rule_set.rules:
            if not self._rule_applies(cursor, rule, config_kind, visibility, pointer_level):
                continue

            # Don't process if empty string
            if rule.prefix:
                if self._verbosity > 1:
                    print(f"  Prefix rule '{rule.name}'; prefix: '{rule.prefix}'")
                prefix_rules.append(rule)
            if rule.suffix:
                if self._verbosity > 1:
                    print(f"  Suffix rule '{rule.name}; suffix: '{rule.suffix}'")
                suffix_rules.append(rule)

            if rule.rule is not None or rule.allow_rule is not None:
                result = self._test_rule(cursor, rule, prefix_rules, suffix_rules, location, substitute_vars)
                if result is not None:
                    return result

        return True

    def process(self, translation_unit: TranslationUnit) -> bool:
        self._process_tokens(translation_unit)
        self._process(translation_unit.cursor)

        for ignore_comment in self._ignore_comments.values():
            if not ignore_comment.used:
                location = ignore_comment.token.location
                print(f"WARNING: {location.file.name}:{location.line}:{location.column} - ignore comment not used")

        return not self._has_failures
    
    def _process_tokens(self, translation_unit: TranslationUnit) -> None:
        for token in translation_unit.cursor.get_tokens():
            if token.kind == TokenKind.COMMENT:
                match = re.fullmatch(Processor._COMMENT_REGEX, token.spelling) 
                if match is not None:
                    value = (match.group(1) or match.group(2)).strip()
                    location = f"{token.location.file}:{token.location.line}:{token.location.column}"
                    if value == "ignore":
                        line = token.location.line 
                        span_before = translation_unit.get_extent(token.location.file.name,
                                                        ((line, 1),
                                                        (line, 1)))
                        tokens_before = list(translation_unit.get_tokens(extent=span_before))
                        if len(tokens_before) == 0 or (len(tokens_before) == 1 and tokens_before[0].extent == token.extent):
                            line += 1 # Nothing before it
                        self._ignore_comments[f"{token.location.file}:{line}"] = IgnoreComment(token=token)
                    else:
                        print(f"WARNING: {location} - Unrecognised comment '{token.spelling}'")

    def _process(self, cursor: Cursor) -> None:
        passed = self._process_node(cursor)
        if not passed:
            self._has_failures = True

        # Don't recurse into typedefs for enums and structs, as that's a duplicate of recursing into the typedef'd type
        # (which means we'll visit all struct/enum members twice)
        if cursor.kind != CursorKind.TYPEDEF_DECL or cursor.underlying_typedef_type.get_canonical().kind not in [
            TypeKind.RECORD,
            TypeKind.ENUM,
        ]:
            for child in cursor.get_children():
                self._process(child)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("filename", help="Path to the file to process")
    parser.add_argument("-c", "--config", required=True, help="Path to the configuration file")
    parser.add_argument("--libclang", help="Path to libclang.dll, if it isn't in your PATH")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Print debug messages (specify multiple times for more verbosity)",
    )
    args = parser.parse_args()

    if args.libclang:
        Config.set_library_file(args.libclang)

    config = ConfigParser()
    if len(config.read(args.config)) != 1:
        raise Exception(f"Unable to open config file '{args.config}'")

    processor = Processor(RuleSet(config), args.verbose)
    index = Index.create()
    translation_unit = index.parse(args.filename)
    passed = processor.process(translation_unit)
    if not passed:
        sys.exit(1)
