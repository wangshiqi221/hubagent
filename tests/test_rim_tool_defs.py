"""轮辋工具声明与真实服务、注册入口的契约检查。"""
import json
import unittest

from agent.tool_defs import RIM_TOOL_DEFINITIONS, TOOL_DEFINITIONS
from rim_reconstruction.adapters.cpp_worker_adapter import PROCESSING_DEFAULTS
from rim_reconstruction.services.section_service import validate_section_parameters
from rim_reconstruction.tool_handlers import rim_process_sections


class RimToolDefinitionTests(unittest.TestCase):
    def test_declared_tool_is_exposed_once(self):
        names = [item["function"]["name"] for item in TOOL_DEFINITIONS]
        self.assertEqual(names.count(rim_process_sections.__name__), 1)
        self.assertEqual(len(names), len(set(names)))
        json.dumps(TOOL_DEFINITIONS, ensure_ascii=False, allow_nan=False)

    def test_properties_match_service_contract(self):
        schema = RIM_TOOL_DEFINITIONS[0]["function"]["parameters"]
        arguments = {
            "task_id": "schema_test", "stl_path": "sample.stl", "unit": "mm",
            "axis_origin": [0, 0, 0], "axis_direction": [0, 0, 1],
            "radial_reference": [1, 0, 0], "angles_degrees": [0, 47, 103],
        }
        self.assertEqual(set(schema["required"]), set(arguments))
        self.assertEqual(set(schema["properties"]), set(arguments) | {"processing"})
        self.assertFalse(schema["additionalProperties"])
        processing = schema["properties"]["processing"]
        self.assertEqual(set(processing["properties"]), set(PROCESSING_DEFAULTS))
        self.assertFalse(processing["additionalProperties"])
        validate_section_parameters(arguments)
        validate_section_parameters({**arguments, "processing": PROCESSING_DEFAULTS})
        for key in schema["required"]:
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_section_parameters({k: v for k, v in arguments.items() if k != key})

    def test_geometry_constraints_are_explicit(self):
        properties = RIM_TOOL_DEFINITIONS[0]["function"]["parameters"]["properties"]
        self.assertEqual(properties["unit"]["enum"], ["mm"])
        for key in ("axis_origin", "axis_direction", "radial_reference"):
            self.assertEqual(properties[key]["minItems"], 3)
            self.assertEqual(properties[key]["maxItems"], 3)
        angles = properties["angles_degrees"]
        self.assertTrue(angles["uniqueItems"])
        self.assertEqual(angles["items"], {
            "type": "number", "minimum": 0, "exclusiveMaximum": 360})
