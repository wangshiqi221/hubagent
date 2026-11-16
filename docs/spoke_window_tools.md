# Agent 辐条窗口工具链

基线位于 GitHub main 提交 e7571e9；本功能将中间曲面/切削体生成与
FreeCAD 阵列减法分离。不是由一个工具直接返回最终轮毂。

## 构建与环境

已有 Python/NumPy/SciPy 负责网格分析；C++ 使用项目现有 Qt Core、OCCT、
SurfaceFit 和 SurfaceJoin。无需引入另一套曲面拟合实现。

在已初始化 MSVC 的终端中：

```powershell
cmake --build stlload_hub/build_surface --target geometry_worker -j 4
```

当前构建产物为 `stlload_hub/build_surface/closed_cutter/geometry_worker.exe`。
首次从GitHub检出时应先按 stlload_hub/CMakeLists.txt 配置本地 Qt/OCCT 路径。
适配器优先查找该构建产物，否则使用已有 Worker 配置。旧 Worker 不支持新操作，
须重新编译。不要将整套 DLL 混入 FreeCAD 进程；运算仍在外部 Worker 中执行。

## 工具顺序

1. `rim_inspect_stl` / `rim_estimate_axis`：获取真实模型和轴线引用。
2. `rim_transverse_sections`：确定有实际窗口的轴向采样层。
3. `rim_analyze_window_regions`：传 axis_call_directory、depths_mm，生成窗口、
   四面 region_id、几何分组及 windows.svg 预览。可选 band_mm（默认3mm）、
   type_count（默认2）、normal_axis_limit（默认0.85）。
4. `rim_refine_surface_region`：如混入圆角/缺少支撑，调整带宽/法向过滤。
   返回新的完整分析；不修改原文件，旧拟合不属于新分析。
5. `rim_fit_surface_patch`：以 analysis_path、region_id 和明确容差拟合一个面。
   对一个窗口调用四次，返回四个 surface_report。
6. `rim_build_window_cutting_solid`：提交同一分析同一窗口的四个不同面报告，
   可选 fillet_radius_mm（默认0）；返回独立 cutter_report，不做阵列/减法。
   axial_margin_mm 可显式增大端盖余量；预览会报告毛坯超出切削体的轴向范围，
   未贯穿时拒绝阵列，不能把局部体积减少误认为开窗完成。
7. 对B类窗口重复步骤5、6，不用A类种子冒充另一类几何。
8. `rim_analyze_window_pattern`：读取分组及角度假设。
9. `rim_list_spoke_objects`：读取现有文档、回转毛坯的真实对象名。
10. `rim_import_window_cutter`：将 A/B BREP 产物导入 FreeCAD。
11. `rim_validate_window_pattern`：验证旋转后切削体边界与目标STL窗口壁采样
    的距离；match_tolerance_mm 默认0.5mm，是采样匹配阈值，不是拟合容差。
12. `rim_preview_window_pattern`：检查各实例与毛坯的体积交集和单次切割。
13. `rim_create_cutter_pattern`：使用同一毛坯、种子和角度列表生成持久化阵列。
14. `rim_cut_spoke_windows`：给出 blank_name、pattern_names，事务内顺序执行
    原生 Part::Cut。失败回滚；必须保持有效单实体且实际减少体积。

CAD工具支持可选 document_name；数组角度为相对种子当前原始位置的旋转角，
不是模型中窗口的绝对周向位置。分析报告同时提供绝对角与 relative_angles_degrees。

## 阶段和来源

界面新增“辐条窗口重建”模式，只有最终窗口布尔切割成功才能完成该模式。
回转毛坯、曲面拟合、切削体或阵列的成功都不能替代最终完成条件。
后台分析与C++运算由 RimToolWorker 运行，可取消；FreeCAD文档操作只在主线程。

所有选区保存真实三角形索引和源STL SHA256。拟合点由程序重新读取原网格，
包含顶点、边中点和三角形中心，不接受模型提交点列。报告/BREP带文件摘要，
四面必须属于同一窗口。源网格或分析文件变化后旧引用被拒绝。

导入保留全局坐标；工具沿用毛坯轴线，不再独立估计。移动过的毛坯或不同来源
轴线被拒绝，避免悄悄居中/旋转。阵列使用持久化 Python 特征，减法为原生Part特征。

## 首版自动选区边界

自动选区是几何候选生成，不是任意模型的语义识别保证：
- 多层横截面按闭合环嵌套找孔洞，排除近轴孔和微小碎环；
- 当前微小环面积阈值为外半径平方的0.0005；
- 采用径向/切向边界极值构造四段，无法形成四侧支撑时不凑面；
- 以局部空间带宽、法向与轴的夹角筛选窗口壁，排除角点附近过渡；
- A/B类型由面积、径向跨度、切向跨度和位置描述确定性聚类产生；
- 等分周期、相位均为假设，不能绕过三维采样匹配检查；
- 预览相交/连通性检查不证明工程强度，窗口壁采样匹配不证明完整连续误差。

明显缺面、开放网格、复杂圆角、非四边窗口或错误轴线可能需要改变采样层和选区，
工具会保留诊断而不捏造几何。当前还未自动识别轮缘保护区、工程最小辐条厚度，
不能把布尔有效等同于工程验收。

## 验证

```powershell
python -m unittest tests.test_window_tools
D:/FreeCAD1.1/bin/python.exe -B -m unittest tests.test_window_tools
```

覆盖窗口/四面候选、两类周期、不可分类型、真实C++四面拟合与封闭实体、
两种切削体的FreeCAD阵列/减法、保存重开重算及阶段完成门禁。
真实源模型已能产生10个窗口候选与A/B各5个、72度周期假设；
这不表示真实模型全部曲面拟合及最终切割已通过。
