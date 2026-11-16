# 从拟合参数创建 FreeCAD 特征树

当前目标是去除中心轮盘局部凹窝、凸台后的基础回转体：中心封闭，
保留轮辋大内腔、轮缘和胎圈座。绕源轮毂中心轴旋转360度。

分析首先选择连续材料截面，再运行 geometry/base_profile_reconstructor.py：
- 保留全部轴向交点及材料区间；局部多交点只标记对应区间，保留相邻观测。
- 近孔处需找到所有角度共有的连续5个单区间观测，作为基础表面跟踪起点。
  允许跳过的小过渡带有范围上限，不能跨过大空腔寻找封孔位置。
- 默认处理范围为外半径的65%；可通过 disc_radius_mm 指定明确边界。
  该分区是保守启发式，不能代表已识别工程语义。
- 对局部分支事件/尖锐凹凸，用至少两个相隔15度以上且在对应区域未修复的
  平顺观测作为证据。选择弯曲变化较小的参考，用C1肩部修正连接原轮廓。
  角度间平顺的整体差异保留为独立候选，不要求各角度逐点重合。
- 同步异常没有其他角度的可靠表面支持时仍返回待检查；不直接合并多层材料。
- 外围轮辋先跨角度检查轴向边界的局部弯曲特征，消除平移/线性倾斜影响。
  以中位数/MAD和独立角度证据识别局部异常，只排除该角度最终轮辋候选；
  该角度可用的中心区观测仍保留。通过检查后使用完整源路径，不拼接包络。
  共有的轮缘/胎圈座/环槽保留，不取凸包、不填满内腔。
  检测幅度下限是5倍feature_tolerance_mm与共同外半径0.5%的较大值，
  不是拟合容差；低于此尺度的细小形状不保证被排除。
  候选按轮辋局部形状与跨角度共识的RMS差异优先排序。
- 轮盘两侧以轴向坐标恒定的径向直线接到中心轴，形成平面封孔；与相邻
  曲线强制分段。补线用精确一次表示，导入Sketcher原生LineSegment，
  添加垂直约束；轴上闭合线添加水平约束。中心补线不参与平滑拟合。

feature_tolerance_mm 默认0.08mm，用于局部变化幅度及最小厚度检查，独立于拟合容差。
分析文件记录 base_disc_v4 版本、轮辋检查证据、跟踪起点、局部分支事件、参考角度和修改范围。
旧v1/v2/v3分析及拟合需重新生成，不能混用v4重放校验。min_support_ratio用于原始轮廓
的匹配标记与排序，并非要求有效角度数/总角度数达到该比例。
生成段索引属于重建边界，不能冒充原STL点索引。拟合前从Worker截面
确定性重放全部主体重建，并逐候选比对；生成段和原始段分别校验。
所有片段必须整体拟合并通过有效面、回转单实体检查；失败不自动放宽容差。

误差评估保留原有全局统计，附加轮辋保留区/轮盘重建区统计。
后者包含主动改变的几何，不应作为原始STL拟合精度的验收指标。

在 FreeCAD 的 Python 控制台调用（必须在主线程执行）：

```python
import sys
sys.path.insert(0, "E:/hubagent/CadAgent-main")
from rim_reconstruction.cad.freecad_rim_builder import build_rim_from_fit

result = build_rim_from_fit("E:/实际调用目录/fits/fit_实际编号.json")
doc = App.getDocument(result["document_name"])
Gui.activeDocument().activeView().viewIsometric()
Gui.activeDocument().activeView().fitAll()
# 选择需要保存的位置；请勿覆盖已有文件。
doc.saveAs("E:/实际输出目录/Rim.FCStd")
```

也可以在 FreeCAD 宏管理器打开并运行项目中的
`scripts/build_rim_from_fit.FCMacro`，通过文件选择器选择完整结果。
宏不会自动保存文件，建模成功后可用 FreeCAD 的保存功能。

生成原生特征：

```text
RimBody (PartDesign::Body)
├─ RimProfile (Sketcher::SketchObject)
└─ RimRevolution (PartDesign::Revolution)
```

草图局部 X=轴向、Y=径向，绕 H_Axis 回转 360°。默认读取同次 Worker 调用
目录的 request.json，将 Body 放到原轴线上。独立 JSON 没有源请求时默认全局
Z 轴、原点和全局 X 径向。也可一起提供 origin、axis_direction、radial_direction。

输入必须为 fitting_service 保存的完整成功 JSON，不能传工具摘要。
采用节点、重数和控制点精确建立曲线，不对采样点重新拟合。周期 SciPy 曲线
以等价的非周期完整系数表示导入，再裁到有效参数范围；添加接缝端点约束。

片段按输入顺序组成单一闭合材料边界。缺口大于默认 1e-7 mm、负半径、
无效面或无法生成单实体时拒绝建模；不会补线构造不存在的边界。
小于连接容差的接缝可由 Coincident 约束微调，不能据此声称原拟合误差未变。

草图保留自由度，能够编辑控制点，回转角度可修改并重算；它还不是由轮辋
宽度、壁厚等工程尺寸驱动的约束草图。实体有效性也不证明轮辋语义完整、
尖角保持或相对 STL 的误差达标。导出 STEP 不保留该特征树，应同时保存 FCStd。

真实内核测试命令（本机安装）：

```powershell
D:/FreeCAD1.1/bin/python.exe -B -m unittest tests.test_freecad_rim_builder -v
```

该模块已通过 `rim_build_revolution` 注册为 Agent 工具，在 FreeCAD 主线程执行，
不进入用于 NumPy/SciPy 算法的后台 RimToolWorker。

在 CadAgent 面板选择“轮辋逆向重建”，提供 STL 路径和重建任务。
程序自动检查模型、调用 C++ 估计轴线、生成径向和截面角度。流程为截面 → 分析 → 模型选择并拟合 →
草图及回转实体。模型只提交候选和片段 ID，无人工确认步骤。
完整模式每轮只执行一个工具，绑定当前产物引用；只有实体工具成功才能结束为成功。
如果材料截面不完整，工具返回错误，不会自动补线创建虚假边界。
