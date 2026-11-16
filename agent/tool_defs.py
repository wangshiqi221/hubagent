"""
工具定义：供大模型工具调用使用的 JSON Schema。

定义 Agent 可用的工具列表，作为 OpenAI-compatible API 的 tools 参数传入。
"""


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": (
                "执行 CadQuery 风格的 Python 代码来创建或修改三维几何体。cq 模块已预先注入，请使用 cq.Workplane 链式 API。使用 cq_show(result, 'Label') 在视口中显示几何体。也可使用 FreeCAD、Part 和 math。变量会在多次调用之间保留，可直接复用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "CadQuery 风格的 Python 代码。使用 cq.Workplane 链式 API，并通过 cq_show(result, 'Label') 显示几何体。也可使用 FreeCAD、Part 和 math。"
                        )
                    },
                    "description": {
                        "type": "string",
                        "description": '简要说明这段代码的作用。'
                    },
                    "document": {
                        "type": "string",
                        "description": '目标文档名称，可选；默认使用 FreeCAD.ActiveDocument。'
                    }
                },
                "required": ["code", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "undo_last",
            "description": (
                '通过恢复文档快照来撤销上一次 execute_code 操作。当代码生成的几何体不正确或发生错误时使用。'
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "capture_view",
            "description": (
                '截取当前 FreeCAD 三维视口，并使用视觉模型分析截图。可在创建或修改几何体后进行外观检查、检查可见问题，或获取当前模型状态的描述。'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            '希望视觉模型针对截图回答的问题。例如：“这个法兰的外观看起来是否正确？”或“检查螺栓孔是否均匀分布。”'
                        )
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": (
                '使用视觉模型分析用户上传的图片，包括参考图纸、草图或照片。当用户提供图片作为建模参考时使用，返回图片内容的详细描述。'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": '待分析图片文件的路径。'
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            '希望视觉模型针对图片回答的问题。例如：“描述这个机械零件的尺寸。”或“这个设计有哪些关键特征？”'
                        )
                    }
                },
                "required": ["image_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "export_step",
            "description": (
                '将当前 FreeCAD 文档导出为 STEP、IGES、STL 或 OBJ 文件，用于保存最终设计以供制造或与其他 CAD 软件交换。CAD 数据交换建议使用 STEP，三维打印可使用 STL/OBJ。导出前会执行质量检查，结果中包含警告信息。'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": (
                            '输出文件路径，扩展名应与导出格式一致（.step/.stp、.iges/.igs、.stl、.obj）。'
                        )
                    },
                    "format": {
                        "type": "string",
                        "description": '导出格式。',
                        "enum": ["step", "iges", "stl", "obj"],
                    },
                    "document": {
                        "type": "string",
                        "description": '目标文档名称，可选；默认使用 FreeCAD.ActiveDocument。'
                    }
                },
                "required": ["filename"]
            }
        }
    },
]

# 轮辋逆向扩展：声明已注册的 rim_process_sections，与截面服务的参数契约一致。
# 只传请求参数；完整轮廓点列保留在本地产物中，不通过模型生成或修改。
RIM_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "rim_process_sections",
            "description": (
                "调用现有 C++ Worker，一次提取 STL 多个角度的处理后有序半平面截面。"
                "返回各角度轮廓统计、警告和本地产物引用，不返回完整点列。"
                "路径、单位及轴线必须来自用户明确输入或已提供的任务配置，不得猜测轴线。"
                "缺少轴线信息时先询问用户。成功只代表截面提取成功，"
                "不代表轮辋识别、拟合、闭合或 CAD 重建完成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string", "minLength": 1,
                        "description": "当前任务的非空标识。"
                    },
                    "stl_path": {
                        "type": "string", "minLength": 1,
                        "description": "已明确提供的 STL 路径；相对路径基于 Worker 配置的 base_directory。"
                    },
                    "unit": {
                        "type": "string", "enum": ["mm"],
                        "description": "输入坐标按毫米解释，不会自动缩放 STL。"
                    },
                    "axis_origin": {
                        "type": "array", "items": {"type": "number"},
                        "minItems": 3, "maxItems": 3,
                        "description": "明确给定的回转轴上一点 [x,y,z]，单位 mm。"
                    },
                    "axis_direction": {
                        "type": "array", "items": {"type": "number"},
                        "minItems": 3, "maxItems": 3,
                        "description": "回转轴非零方向 [dx,dy,dz]；服务层归一化。"
                    },
                    "radial_reference": {
                        "type": "array", "items": {"type": "number"},
                        "minItems": 3, "maxItems": 3,
                        "description": (
                            "零角度的非零径向方向，必须与轴正交；服务层归一化。"
                            "正角度按 axis_direction × radial_reference 方向旋转。"
                        )
                    },
                    "angles_degrees": {
                        "type": "array", "minItems": 1, "maxItems": 360,
                        "uniqueItems": True,
                        "items": {"type": "number", "minimum": 0, "exclusiveMaximum": 360},
                        "description": "1～360 个不重复的 [0,360) 角度，单位度；一次批量执行。"
                    },
                    "processing": {
                        "type": "object",
                        "description": (
                            "可选清洗参数；省略字段使用服务层默认值。"
                            "bump_support_search_length_mm 必须不小于 bump_reference_length_mm。"
                            "不要为获得成功而自行放宽清洗阈值。"
                        ),
                        "properties": {
                            "duplicate_tolerance_mm": {
                                "type": "number", "minimum": 0,
                                "description": "重复点容差，mm；0 使用现有算法策略。"
                            },
                            "enable_micro_bump_cleaning": {"type": "boolean"},
                            "bump_detection_tolerance_mm": {"type": "number", "exclusiveMinimum": 0},
                            "maximum_bump_width_mm": {"type": "number", "exclusiveMinimum": 0},
                            "maximum_bump_displacement_mm": {"type": "number", "exclusiveMinimum": 0},
                            "bump_reference_length_mm": {"type": "number", "exclusiveMinimum": 0},
                            "bump_support_search_length_mm": {"type": "number", "exclusiveMinimum": 0},
                            "bump_support_residual_mm": {"type": "number", "exclusiveMinimum": 0},
                            "bump_tangent_angle_degrees": {
                                "type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 90
                            }
                        },
                        "additionalProperties": False
                    }
                },
                "required": [
                    "task_id", "stl_path", "unit", "axis_origin",
                    "axis_direction", "radial_reference", "angles_degrees"
                ],
                "additionalProperties": False
            }
        }
    }
]

# 轮辋逆向扩展：现有 AgentLoop 读取 TOOL_DEFINITIONS，将新声明纳入实际请求。
# 后续按工作流筛选工具时，可单独使用 RIM_TOOL_DEFINITIONS。
RIM_TOOL_DEFINITIONS.append({'type': 'function', 'function': {'name': 'rim_analyze_candidates', 'description': '对 rim_process_sections 返回的 call_directory 做基础轮盘截面重建（中心区域跨角度去除局部凹窝/凸台、封中心孔，保留轮辋及大内腔；证据不足返回needs_review）。只读取本地截面，不接受点列。返回分页摘要和完整分析文件路径。status=success 仅表示分析完成，analysis_status=needs_review 时必须说明原因。候选尚未确认，不得宣称轮辋重建成功。', 'parameters': {'type': 'object', 'properties': {'call_directory': {'type': 'string', 'description': '截面工具实际返回的绝对调用目录，不得编造。'}, 'consistency_tolerance_mm': {'type': 'number', 'exclusiveMinimum': 0}, 'min_support_ratio': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}, 'tangent_angle_degrees': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 90}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, 'required': ['call_directory'], 'additionalProperties': False}}})

# Base reconstruction controls: no model-submitted coordinates.
for _definition in RIM_TOOL_DEFINITIONS:
    if _definition["function"]["name"] == "rim_analyze_candidates":
        _definition["function"]["parameters"]["properties"].update({
            "disc_radius_mm": {"type": "number", "exclusiveMinimum": 0,
                "description": "Optional central-disc radial boundary in mm; default conservative inner 65% of outer radius."},
            "feature_tolerance_mm": {"type": "number", "exclusiveMinimum": 0,
                "description": "Local feature amplitude and minimum-thickness threshold, default 0.08 mm; independent of fitting tolerance."}})

# 轮辋逆向扩展：模型仅提交本地分析引用与片段编号，不生成坐标，无人工确认步骤。
RIM_TOOL_DEFINITIONS.append({
    "type": "function",
    "function": {
        "name": "rim_fit_profile",
        "description": "选择分析中的一个完整材料候选的全部片段，自动校验后用现有 Python B 样条拟合。无需用户确认。完整点列和曲线参数保存在本地，只返回误差摘要。含尖角的轮廓应先分段；拟合通过不代表完整轮辋或实体重建通过。",
        "parameters": {
            "type": "object",
            "properties": {
                "analysis_path": {"type": "string", "description": "rim_analyze_candidates 实际返回的 analysis_path"},
                "candidate_id": {"type": "string"},
                "segment_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True},
                "tolerance_mm": {"type": "number", "exclusiveMinimum": 0, "description": "默认 0.08 mm，不得为通过而擅自放宽"},
                "max_control_points": {"type": "integer", "minimum": 4, "maximum": 2048},
                "max_iterations": {"type": "integer", "minimum": 2, "maximum": 64},
                "sample_count": {"type": "integer", "minimum": 2, "maximum": 10000}
            },
            "required": ["analysis_path", "candidate_id", "segment_ids"],
            "additionalProperties": False
        }
    }
})
# 轮辋逆向扩展：闭合校验和实体创建由固定 API 实现，不向模型开放几何脚本。
RIM_TOOL_DEFINITIONS.append({
    "type": "function", "function": {
        "name": "rim_build_revolution",
        "description": "读取 rim_fit_profile 成功返回的 result_path，建立 Body、B样条草图和360度回转特征。必须形成有效闭合材料截面；不补线、不跨缺口。仅实体有效性通过才返回 success，不代表已验证 STL 重建精度。",
        "parameters": {"type": "object", "properties": {
            "fit_path": {"type": "string", "description": "拟合工具实际返回的绝对 result_path"}
        }, "required": ["fit_path"], "additionalProperties": False}
    }
})
# 轮辋逆向扩展：只向模型开放解析产物引用，不开放手填轴坐标。
RIM_TOOL_DEFINITIONS.extend([
    {"type": "function", "function": {"name": "rim_inspect_stl",
     "description": "第一步：使用 C++ 检查 STL，返回统计和调用目录。默认毫米，自动生成任务编号。",
     "parameters": {"type": "object", "properties": {"stl_path": {"type": "string"},
         "unit": {"type": "string", "enum": ["mm"]}}, "required": ["stl_path"], "additionalProperties": False}}},
    
    {"type": "function", "function": {"name": "rim_estimate_axis",
     "description": "读取检查产物，调用 C++ 几何算法估计轴原点、方向；当前为 PCA 候选而非精度认证。",
     "parameters": {"type": "object", "properties": {"inspection_call_directory": {"type": "string"}},
         "required": ["inspection_call_directory"], "additionalProperties": False}}},
    
    {"type": "function", "function": {"name": "rim_process_sections_auto",
     "description": "程序从 C++ 产物取得轴线并计算径向；角度由你依据模型解析摘要和此前截面反馈显式规划。没有固定或默认角度。必须说明选择依据，失败后可调整角度重新批量截面。",
     "parameters": {"type": "object", "properties": {"axis_call_directory": {"type": "string"},
         "angles_degrees": {"type": "array", "minItems": 3, "maxItems": 360, "uniqueItems": True,
             "items": {"type": "number", "minimum": 0, "exclusiveMaximum": 360},
             "description": "由模型选择的本轮完整角度集合，至少3个；重试时如需保留旧角度须一并提交"},
         "angle_selection_reason": {"type": "string", "minLength": 1, "maxLength": 4000,
             "description": "基于检查/轴估计摘要或此前轮廓数量、闭合性、支持角度、覆盖率说明采样策略；不能编造轮辐数"}},
         "required": ["axis_call_directory", "angles_degrees", "angle_selection_reason"], "additionalProperties": False}}}
])
RIM_TOOL_DEFINITIONS.append({"type": "function", "function": {
    "name": "rim_repair_sections", "description": "筛选前复用 C++ 条件闭合算法修补截面。输入真实截面调用目录，沿原轴线和角度重新提取及修补，写入新目录；后续分析使用新 call_directory。无法安全修补的轮廓保留开放，repair_status=needs_review 不代表实体可用。",
    "parameters": {"type": "object", "properties": {"call_directory": {"type": "string"}},
                   "required": ["call_directory"], "additionalProperties": False}}})
TOOL_DEFINITIONS += RIM_TOOL_DEFINITIONS

# Separate from the reconstruction chain: exposed only for explicit evaluation requests.
RIM_EVALUATION_TOOL_DEFINITION = {"type": "function", "function": {
    "name": "rim_evaluate_revolution",
    "description": "仅在用户明确要求评估/比较已生成模型误差时调用。读取当前真实 CAD 几何，仅计算重建模型到参考STL的单向距离，返回 MAE、RMS、P95、采样最大误差，以及0.2、1、5 mm等多个误差阈值内的累计面积比例。默认唯一带来源的轮辋对象及整个源 STL；多对象时须指定名称，可提供独立轮辋 STL 避免轮辐干扰。不重建、不自动验收。",
    "parameters": {"type": "object", "properties": {
        "document_name": {"type": "string", "description": "FreeCAD 内部文档名；省略用活动文档"},
        "object_name": {"type": "string", "description": "内部对象名；省略仅允许唯一带 SourceFitPath 的对象"},
        "reference_stl_path": {"type": "string", "description": "用户提供的已分离参考轮辋 STL 绝对路径；省略读原始来源，不猜路径"},
        "tolerances_mm": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0},
                          "minItems": 1, "maxItems": 32, "uniqueItems": True, "default": [.2, 1., 5.],
                          "description": "误差阈值数组(mm)，默认[0.2,1,5]；分别统计距离≤各阈值的面积比例，不过滤误差点"},
        "sample_count": {"type": "integer", "minimum": 100, "maximum": 100000, "default": 5000, "description": "仅在重建模型表面按面积均匀采样的点数"},
        "mesh_deflection_mm": {"type": "number", "exclusiveMinimum": 0, "default": .02},
        "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": 2026}
    }, "additionalProperties": False}}}
TOOL_DEFINITIONS.append(RIM_EVALUATION_TOOL_DEFINITION)

# 轮辋逆向扩展：完整模式始终声明这六个工具；阶段门禁控制执行，而非隐藏声明。
RIM_TRANSVERSE_TOOL_DEFINITION = {"type":"function","function":{
    "name":"rim_transverse_sections",
    "description":"Python-only STL cross sections perpendicular to the existing hub axis. depths_mm are signed axial distances from axis origin (not global Z or angles). Returns summaries and complete local UV/XYZ contours file; does not identify windows, fit surfaces or build solids. Empty/open/branched sections are reported, not force-closed.",
    "parameters":{"type":"object","properties":{
        "axis_call_directory":{"type":"string","description":"Actual directory returned by rim_estimate_axis"},
        "depths_mm":{"type":"array","items":{"type":"number"},"minItems":1,"maxItems":64,"uniqueItems":True},
        "tolerance_mm":{"type":"number","exclusiveMinimum":0,"default":0.00001}},
        "required":["axis_call_directory","depths_mm"],"additionalProperties":False}}}
RIM_TOOL_DEFINITIONS.append(RIM_TRANSVERSE_TOOL_DEFINITION)
TOOL_DEFINITIONS.append(RIM_TRANSVERSE_TOOL_DEFINITION)

RIM_RECONSTRUCTION_TOOL_NAMES = (
    "rim_inspect_stl", "rim_estimate_axis", "rim_process_sections_auto",
    "rim_repair_sections", "rim_analyze_candidates", "rim_fit_profile", "rim_build_revolution", "rim_transverse_sections",
)

from rim_reconstruction.window_tools import schemas as _window_schemas
WINDOW_TOOL_DEFINITIONS = _window_schemas()
RIM_TOOL_DEFINITIONS.extend(WINDOW_TOOL_DEFINITIONS)
TOOL_DEFINITIONS.extend(WINDOW_TOOL_DEFINITIONS)
RIM_RECONSTRUCTION_TOOL_NAMES += tuple(t["function"]["name"] for t in WINDOW_TOOL_DEFINITIONS)
