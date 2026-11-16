"""Shared argument validation for the schemas actually exposed to agents."""
import math

from orchestra_core.workspace.tool_errors import ToolError


EDIT_TOOL_NAMES = frozenset({'create_file', 'replace_text', 'apply_patch'})


def validate_arguments(tool, arguments):
    """Validate the small JSON Schema subset used by our tool definitions."""
    name = tool['function']['name']

    def reject(field, message):
        raise ToolError('invalid_arguments', f'{name}: {field} {message}')

    def validate(value, schema, field):
        kind = schema['type']
        valid = {
            'object': isinstance(value, dict),
            'array': isinstance(value, list),
            'string': isinstance(value, str),
            'integer': type(value) is int,
            'number': type(value) is int or (type(value) is float and math.isfinite(value)),
            'boolean': type(value) is bool,
        }
        if not valid[kind]:
            reject(field, f'must be {kind}. Check the tool schema.')
        if 'enum' in schema and value not in schema['enum']:
            reject(field, 'must be one of: ' + ', '.join(map(str, schema['enum'])) + '.')
        if kind == 'object':
            properties = schema['properties']
            missing = set(schema.get('required', [])) - value.keys()
            if missing:
                reject(field, 'is missing required fields: ' + ', '.join(sorted(missing)) + '.')
            if schema.get('additionalProperties') is False and value.keys() - properties.keys():
                reject(field, 'contains unexpected fields. Check the tool schema.')
            for key, item in value.items():
                if key in properties:
                    validate(item, properties[key], key)
        elif kind == 'array':
            if len(value) > schema.get('maxItems', float('inf')):
                reject(field, f'must contain at most {schema["maxItems"]} items.')
            if len(value) < schema.get('minItems', 0):
                reject(field, f'must contain at least {schema["minItems"]} items.')
            for item in value:
                validate(item, schema['items'], field + ' item')
        elif kind in {'integer', 'number'}:
            if 'minimum' in schema and value < schema['minimum']:
                reject(field, f'must be at least {schema["minimum"]}.')
            if 'maximum' in schema and value > schema['maximum']:
                reject(field, f'must be at most {schema["maximum"]}.')
        elif kind == 'string':
            if len(value) < schema.get('minLength', 0):
                reject(field, f'must contain at least {schema["minLength"]} characters.')
            if len(value) > schema.get('maxLength', float('inf')):
                reject(field, f'must contain at most {schema["maxLength"]} characters.')

    validate(arguments, tool['function']['parameters'], 'arguments')
