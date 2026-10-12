"""Pure-logic checks for configurable checklist templates. No Qt, no IO, no network."""
import unittest

from itmanager.checklist_templates import (
    BUILTIN_TEMPLATES, GENERIC_SCOPE, SAFE_TEMPLATE_ERRORS, default_templates,
    export_template, item_names, module_names, scope_label, template_names,
    validate_template,
)


def _template(**overrides):
    value = {'name': '自定义模板', 'description': '说明', 'scope': GENERIC_SCOPE,
             'items': [{'name': '检查项一', 'hint': '通过判定：有证据', 'required': True},
                       {'name': '检查项二', 'hint': '', 'required': False}],
             'enabled': True, 'version': 1}
    value.update(overrides)
    return value


class BuiltinTemplateTests(unittest.TestCase):
    def test_builtin_templates_are_valid_and_keep_original_names(self):
        self.assertEqual(len(BUILTIN_TEMPLATES), 4)
        self.assertEqual([row['name'] for row in BUILTIN_TEMPLATES],
                         ['备份运行', '入离职权限', '合同续费', '周度项目检查'])
        names = [row['name'] for row in BUILTIN_TEMPLATES]
        for template in default_templates():
            validate_template(template, existing=names, allowed=[template['name']])
            self.assertTrue(template['enabled'])
            self.assertEqual(template['version'], 1)

    def test_every_builtin_item_has_hint_and_three_items(self):
        for template in default_templates():
            self.assertGreaterEqual(len(template['items']), 3)
            self.assertEqual(len(template['items']), len(set(item_names(template))))
            for item in template['items']:
                self.assertIn('通过判定', item['hint'])
                self.assertIs(type(item['required']), bool)

    def test_scope_values_reference_real_modules_or_generic(self):
        known = set(module_names())
        for template in default_templates():
            self.assertIn(template['scope'], known)
            self.assertNotEqual(scope_label(template['scope']), template['scope'])
        validate_template(_template(scope=GENERIC_SCOPE))
        validate_template(_template(scope='%s、%s' % (module_names()[0], module_names()[1])))
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(scope=GENERIC_SCOPE + '、不存在模块'))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[3])


class NameValidationTests(unittest.TestCase):
    def test_valid_template_passes(self):
        self.assertIsNone(validate_template(_template()))

    def test_name_must_be_non_empty_string_within_limit(self):
        for bad in (None, '', '   ', 5, [], {}, '名' * 61):
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(name=bad))
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[0])

    def test_name_control_characters_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(name='备份\x00运行'))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[0])

    def test_name_duplicate_reported_with_its_own_message(self):
        names = [row['name'] for row in BUILTIN_TEMPLATES]
        for duplicate in names:
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(name=duplicate), existing=names)
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[1])
        # The same name is fine when no existing list is supplied, or when it is its own.
        validate_template(_template(name=names[0]))
        validate_template(_template(name=names[0]), existing=['另一个模板'])

    def test_template_must_be_a_mapping(self):
        for bad in (None, [], '备份运行', 4):
            with self.assertRaises(ValueError) as caught:
                validate_template(bad)
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[0])

    def test_missing_name_is_invalid(self):
        with self.assertRaises(ValueError) as caught:
            validate_template({'items': [{'name': '检查项一'}]})
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[0])


class FieldValidationTests(unittest.TestCase):
    def test_description_limit(self):
        validate_template(_template(description='说' * 2000))
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(description='说' * 2001))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[2])
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(description=123))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[2])

    def test_scope_must_reference_a_module_or_generic(self):
        validate_template(_template(scope=''))
        self.assertEqual(scope_label(''), GENERIC_SCOPE)
        for bad in ('财务共享中心', '不存在模块', 5, '名' * 201):
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(scope=bad))
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[3])

    def test_enabled_must_be_boolean(self):
        validate_template(_template(enabled=False))
        for bad in ('true', 1, 0, None):
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(enabled=bad))
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[9])

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(script='print(1)'))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[8])
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(version='1'))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[0])


class ItemValidationTests(unittest.TestCase):
    def test_item_count_bounds(self):
        for bad in ([], 'x', None, [{'name': '项%d' % i} for i in range(51)]):
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(items=bad))
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[4])
        validate_template(_template(items=[{'name': '项%d' % i} for i in range(50)]))

    def test_item_name_invalid_or_duplicate(self):
        for bad in ([{'name': ''}], [{'name': '  '}], [{'name': 7}], [{'name': '项' * 121}],
                    [{'name': '重复'}, {'name': '重复'}]):
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(items=bad))
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[5])

    def test_item_hint_limit(self):
        validate_template(_template(items=[{'name': '项', 'hint': '提' * 1000}]))
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(items=[{'name': '项', 'hint': '提' * 1001}]))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[6])

    def test_item_required_must_be_boolean(self):
        validate_template(_template(items=[{'name': '项', 'required': True}]))
        for bad in ('yes', 1, 0, None, [True]):
            with self.assertRaises(ValueError) as caught:
                validate_template(_template(items=[{'name': '项', 'required': bad}]))
            self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[7])

    def test_item_unknown_fields_are_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(items=[{'name': '项', 'command': 'rm -rf'}]))
        self.assertEqual(str(caught.exception), SAFE_TEMPLATE_ERRORS[8])

    def test_errors_never_echo_user_input(self):
        secret = '机密-<script>alert(1)</script>'
        with self.assertRaises(ValueError) as caught:
            validate_template(_template(items=[{'name': secret + '项' * 200}]))
        self.assertNotIn('<script>', str(caught.exception))
        self.assertIn(str(caught.exception), SAFE_TEMPLATE_ERRORS)


class HelperTests(unittest.TestCase):
    def test_default_templates_are_deep_copies(self):
        first = default_templates()
        first[0]['name'] = '被改动'
        first[0]['items'][0]['name'] = '被改动项'
        first[0]['items'].append({'name': '多余', 'hint': '', 'required': False})
        second = default_templates()
        self.assertEqual(second[0]['name'], BUILTIN_TEMPLATES[0]['name'])
        self.assertEqual(second[0]['items'][0]['name'], BUILTIN_TEMPLATES[0]['items'][0]['name'])
        self.assertEqual([len(row['items']) for row in second],
                         [len(row['items']) for row in BUILTIN_TEMPLATES])
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[0]['items'][0], second[0]['items'][0])

    def test_template_and_item_names(self):
        rows = [{'name': '甲', 'items': [{'name': '一'}, {'name': '二'}]}, {'name': '乙', 'items': []}]
        self.assertEqual(template_names(rows), ['甲', '乙'])
        self.assertEqual(item_names(rows[0]), ['一', '二'])
        self.assertEqual(item_names(rows[1]), [])
        self.assertEqual(template_names(None), [])
        self.assertEqual(item_names({}), [])

    def test_export_template_excludes_internal_fields(self):
        template = dict(_template(), id='checklist-tpl-9', oldhistory=[{'name': '旧'}],
                        created='2026-01-01', updated='2026-02-02')
        exported = export_template(template)
        self.assertEqual(set(exported), {'name', 'description', 'scope', 'enabled', 'items'})
        self.assertEqual(set(exported['items'][0]), {'name', 'hint', 'required'})
        self.assertNotIn('version', exported)
        self.assertNotIn('id', exported)
        validate_template(exported)
        exported['items'][0]['name'] = '改动'
        self.assertNotEqual(exported['items'][0]['name'], template['items'][0]['name'])

    def test_export_template_of_unknown_shape_is_still_config_only(self):
        exported = export_template(None)
        self.assertEqual(exported['items'], [])
        self.assertTrue(exported['enabled'])


if __name__ == '__main__':
    unittest.main()
