"""Conservative per-turn opt-in; previous user messages cannot authorize evaluation."""
import re


def requests_evaluation(text):
    text = text.strip().lower()
    if re.search(r"不(?:要|用|需|必|进行|做|再)[^，,。；;\n]{0,12}(评估|评价|评测|检测|比较|对比|误差)|(?:取消|停止|暂缓)[^，,。；;\n]{0,12}(评估|评测)|\b(?:don't|do not|no need to)\b.*?evaluat", text):
        return False
    if re.search(r"(?:如何|怎么|怎样|什么是|是什么|有哪些|解释|介绍|方案|代码|实现|新增|添加|功能|注册|工具|时候|如果|仅在|只有|是否)", text):
        return False
    return bool(re.search(r"评估|评测|评价.{0,8}(?:模型|轮辋)|(?:检查|检测).{0,16}(?:模型|轮辋)|(?:比较|对比|计算|测量|看看|看一下|分析).{0,24}(?:模型.{0,8}(?:好坏|质量|精度)|误差|偏差|精度)|\b(?:evaluate|assess|measure|compare)\b.{0,60}\b(?:model|rim|error|accuracy)\b", text))


EVALUATION_PROMPT = """用户明确请求评估现有模型。本次任务使用 rim_evaluate_revolution，禁止为评估重新创建模型或调用重建流程。
优先调用工具使用当前唯一重建对象和来源记录；名称或参考不明确时按工具错误说明所缺信息，不猜路径或对象名。
用户未指定参数时用工具默认值并在回复中说明。先调用实际工具，不能凭聊天历史编造数据。
仅返回重建模型→参考STL的单向 MAE、RMS、P95、采样最大误差(mm)，并逐项报告area_ratios中各误差阈值内的面积比例(%)，默认阈值为0.2、1、5 mm，同时说明采样数和离散精度。
多个阈值复用同一组距离；面积比例表示误差≤阈值的累计比例，不是分段比例。不报告覆盖率或参考完整率。
不计算参考→模型或双向最大误差；单向贴合误差不能证明重建完整，最近点也可能落在非轮辋结构。
reference_scope=entire_source_stl_unsegmented 时必须提示包含轮辐等非轮辋区域的影响，不能称纯轮辋精度。
不得隐藏 warnings，不把 status=success 当成精度通过；compliant=null 表示未作工程验收。
错误/取消必须如实报告，不生成虚构指标。评估只在本轮用户要求时执行，完成后反馈结果。
"""
