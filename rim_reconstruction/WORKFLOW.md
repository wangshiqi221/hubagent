# Agent 完整轮辋工作流

界面仅提供两种模式：**通用 CAD 建模**、**轮辋逆向重建**。
选择轮辋逆向重建后，只需提供 STL 路径和任务，例如：
“请重建 E:/hub_stl/hubmain3processed.stl 的轮辋回转主体。”
无需手填轴线、径向参考、角度和任务编号。角度由模型依据几何摘要规划，默认毫米，不自动缩放。

工具顺序：

1. `rim_inspect_stl`：C++ 检查 STL，程序自动生成任务编号。
2. `rim_estimate_axis`：调用 C++ estimate_axis 取得轴原点、方向。
3. `rim_process_sections_auto`：读取轴线产物，程序生成正交径向；模型必须显式提供角度列表及选择理由，没有默认角度。
4. `rim_analyze_candidates`：读取截面产物并分析。
5. `rim_fit_profile`：模型根据摘要选择候选及片段，Python 拟合。
6. `rim_build_revolution`：读取实际拟合产物，建立原生草图和回转实体。

当前 C++ 轴算法仅提供 PCA 初始化，`axis_validated=false`。允许用于初始试建，
但这不是轴线精度保证，不能将实体有效误认为轴线正确。更强的轴线拟合/验证
仍需在几何算法层补充；界面不再常规索取手动轴线参数。

角度规划先依赖检查与轴线摘要进行探索性采样，再根据截面闭合情况、跨角度
支持率和覆盖情况调整。当前摘要不能直接确定轮辐相位，不能声称模型首次采样
就识别或避开了轮辐。每次重切提交完整新角度集合，旧下游产物状态会失效。

模型选择与拟合合并在第五个工具，无人工确认或确认弹窗。原提案/人工确认工具
不在当前方案中，不注册 `rim_propose_selection` 和 `rim_fit_confirmed_profile`。
模型不提交完整点列、不生成几何脚本。完整模式始终声明六个工具，按阶段校验执行条件，执行前再次验证
当前产物引用；任何上游重试都会使下游状态失效。每轮仅执行一个工具。

截面、分析和拟合在后台 `RimToolWorker` 中运行；实体工具走面板主线程的
`dispatch_tool`。创建实体期间不强制中断 FreeCAD 内核操作；操作完成后才可
处理 Qt 排队的停止请求。后台任务保留原有取消和迟到回调保护。

只有成功创建有效单实体、草图和回转特征，完整模式才标记成功。无候选、
不闭合或拟合失败会报告失败；不自动填补缺口。有效实体不保证轮辋语义完整、
工程尺寸约束齐备或相对 STL 的精度通过。

本机 FreeCAD 1.1 携带 SciPy 1.16，周期拟合自动采用兼容的 SciPy FITPACK
接口，仍执行原来的误差检查。Windows 启动 Worker 时，临时清除继承的
DLL 搜索目录，CreateProcess 后立即恢复，避免加载 FreeCAD 的不匹配依赖。

运行已安装工作台前，请将当前源码同步到实际的 FreeCAD Mod/CadAgent，
保留自己的 API 配置并确认 Worker 配置有效，然后重启 FreeCAD。
本机检测到另有已安装副本：
`C:/Users/Wang/AppData/Roaming/FreeCAD/v1-1/Mod/CadAgent`。
本次修改的是 `E:/hubagent/CadAgent-main`，未改动安装副本。

测试：

```powershell
python -B -m unittest tests.test_rim_pipeline tests.test_rim_workflow tests.test_rim_panel_cancellation
D:/FreeCAD1.1/bin/python.exe -B -m unittest tests.test_rim_end_to_end tests.test_rim_cad_tool tests.test_freecad_rim_builder
```

端到端测试使用合成双回转环网格，运行真实 Worker、真实筛选/拟合和 FreeCAD
内核；候选选择为确定性测试替身，不调用实际 DeepSeek。Qt 聊天交互与实际扫描
轮毂仍需安装后验证。
