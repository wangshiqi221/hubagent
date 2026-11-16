"""轮辋逆向扩展：完整重建的确定性阶段门禁，与 Qt/FreeCAD 无关。"""
import json


class RimPipeline:
    def __init__(self, require_spokes=False):
        self.require_spokes = require_spokes
        self.spoke_solid = None
        self.window_artifacts = []
        self.inspection = None
        self.axis = None
        self.section = None
        self.analysis = None
        self.fit = None
        self.solid = None
        self.last_tool_failed = False

    def next_step(self):
        """仅在已有成功输入且无真实工具错误时提示推进，不掩盖失败或无候选。"""
        if self.solid and self.require_spokes and not self.spoke_solid and not self.last_tool_failed:
            return "rim_analyze_window_regions" if not self.window_artifacts else None
        if self.last_tool_failed or not self.inspection or self.solid:
            return None
        if not self.axis:
            return "rim_estimate_axis"
        if not self.section:
            return "rim_process_sections_auto"
        if not self.analysis:
            return "rim_analyze_candidates"
        if self.analysis.get("analysis_status") != "candidates_found":
            return None
        return "rim_fit_profile" if not self.fit else "rim_build_revolution"

    def request_context(self):
        """逐轮注入精简的确定性状态，不包含任何几何点列。"""
        references = {}
        for label, result, key in (("inspection_call_directory", self.inspection, "call_directory"),
                                   ("axis_call_directory", self.axis, "call_directory"),
                                   ("call_directory", self.section, "call_directory"),
                                   ("analysis_path", self.analysis, "analysis_path"),
                                   ("fit_path", self.fit, "result_path")):
            if result:
                references[label] = result.get(key)
        if self.solid:
            references.update(document_name=self.solid.get("document_name"), blank_name=self.solid.get("body_name"))
        return {"executable_tools": sorted(self.allowed_tools()), "next_step": self.next_step(),
                "window_artifacts": self.window_artifacts[-64:],
                "artifacts": references, "last_tool_failed": self.last_tool_failed}

    def allowed_tools(self):
        names = {"rim_inspect_stl"}
        if self.inspection:
            names.add("rim_estimate_axis")
        if self.axis:
            names.update(("rim_process_sections_auto", "rim_transverse_sections"))
            from rim_reconstruction.window_tools import BACKGROUND, MAIN
            names.update(BACKGROUND)
            names.update(MAIN)
        if self.section:
            names.update(("rim_analyze_candidates", "rim_repair_sections"))
        if self.analysis and self.analysis.get("analysis_status") == "candidates_found":
            names.add("rim_fit_profile")
        if self.fit:
            names.add("rim_build_revolution")
        return names

    def validate_call(self, name, arguments):
        if name not in self.allowed_tools():
            return "工具不满足阶段前置条件，请先完成截面、分析、拟合，再创建实体"
        try:
            data = json.loads(arguments)
            if not isinstance(data, dict):
                raise ValueError("工具参数必须为对象")
            if name == "rim_estimate_axis" and data.get("inspection_call_directory") != self.inspection["call_directory"]:
                raise ValueError("必须使用本轮模型检查返回的目录")
            if name in ("rim_process_sections_auto", "rim_transverse_sections") and data.get("axis_call_directory") != self.axis["call_directory"]:
                raise ValueError("必须使用本轮 C++ 轴线估计返回的目录")
            if name in ("rim_analyze_candidates", "rim_repair_sections") and data.get("call_directory") != self.section["call_directory"]:
                raise ValueError("必须使用本轮截面返回的 call_directory")
            if name == "rim_fit_profile" and data.get("analysis_path") != self.analysis["analysis_path"]:
                raise ValueError("必须使用当前分析返回的 analysis_path")
            if name == "rim_build_revolution" and data.get("fit_path") != self.fit["result_path"]:
                raise ValueError("必须使用当前成功拟合返回的 result_path")
        except (ValueError, TypeError, KeyError) as exc:
            return str(exc)
        return None

    def record(self, execution):
        name = execution.tool_name
        from rim_reconstruction.window_tools import BACKGROUND
        if name in BACKGROUND or name in ("rim_import_window_cutter", "rim_create_cutter_pattern", "rim_validate_window_pattern"):
            self.spoke_solid = None
        self.last_tool_failed = True
        # 上游重试立即撤销下游状态，包括失败和取消，不能复用旧实体报成功。
        if name == "rim_inspect_stl":
            self.inspection = self.axis = self.section = self.analysis = self.fit = self.solid = None
        elif name == "rim_estimate_axis":
            self.axis = self.section = self.analysis = self.fit = self.solid = None
        elif name in ("rim_repair_sections", "rim_process_sections", "rim_process_sections_auto"):
            self.section = self.analysis = self.fit = self.solid = None
        elif name == "rim_analyze_candidates":
            self.analysis = self.fit = self.solid = None
        elif name == "rim_fit_profile":
            self.fit = self.solid = None
        elif name == "rim_build_revolution":
            self.solid = None
        if name == "rim_cut_spoke_windows" or name in ("rim_inspect_stl", "rim_estimate_axis", "rim_build_revolution"):
            self.spoke_solid = None
        try:
            data = json.loads(execution.result)
            if execution.is_error or not isinstance(data, dict) or data.get("status") != "success":
                return
            self.last_tool_failed = False
            if name.startswith("rim_"):
                refs={k:data[k] for k in ("analysis_path","surface_report","cutter_report","document_name","cutter_name","pattern_name","object_name") if data.get(k)}
                if refs:self.window_artifacts.append({"tool":name,**refs})
            if name == "rim_cut_spoke_windows" and data.get("operation") == "cut_spoke_windows" and data.get("solid_count") == 1 and data.get("volume_mm3", 0) > 0:
                self.spoke_solid = data
            if (name == "rim_inspect_stl" and data.get("operation") == "inspect_stl"
                    and data.get("call_directory") and data.get("task_id")):
                self.inspection = data
            elif (name == "rim_estimate_axis" and self.inspection
                  and data.get("operation") == "estimate_axis"
                  and data.get("task_id") == self.inspection["task_id"] and data.get("call_directory")):
                self.axis = data
            elif (name in ("rim_repair_sections", "rim_process_sections", "rim_process_sections_auto") and data.get("operation") == "process_sections"
                    and data.get("sections") and data.get("section_artifacts") and data.get("call_directory")):
                self.section = data
            elif (name == "rim_analyze_candidates" and self.section
                  and data.get("operation") == "analyze_rim_candidates"
                  and data.get("call_directory") == self.section["call_directory"]
                  and data.get("analysis_path") and data.get("analysis_id")):
                self.analysis = data
            elif (name == "rim_fit_profile" and self.analysis
                  and data.get("operation") == "fit_profile"
                  and data.get("analysis_id") == self.analysis["analysis_id"]
                  and data.get("fit_success") is True and data.get("compliant") is True
                  and data.get("result_path") and data.get("fit_id")):
                self.fit = data
            elif (name == "rim_build_revolution" and self.fit
                  and data.get("operation") == "build_rim_revolution"
                  and data.get("fit_id") == self.fit["fit_id"]
                  and data.get("solid_count") == 1 and data.get("volume_mm3", 0) > 0
                  and all(data.get(k) for k in ("document_name", "body_name", "sketch_name", "revolution_name"))):
                self.solid = data
        except (ValueError, TypeError, KeyError):
            return

    def completion(self):
        if self.require_spokes:
            return self.spoke_solid is not None, "辐条重建必须成功完成窗口阵列布尔切割；毛坯或切削体不能替代最终结果。"
        return (self.solid is not None,
                "完整重建要求本轮成功建立草图和回转单实体；截面、分析或拟合成功不能替代。")
