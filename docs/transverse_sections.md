# Python 横向截面工具

Agent 工具名：`rim_transverse_sections`。读取 `rim_estimate_axis` 返回的
`axis_call_directory`，直接用 Python/NumPy 读取 STL 并求三角形与平面交线。
不启动 C++ Worker，不访问 FreeCAD 文档。轴线来源沿用现有解析结果，
不会因此变成已经验证的工程轴线。

参数示例（目录必须换成工具实际返回值，深度根据模型选择）：

```json
{
  "axis_call_directory": "E:/实际调用目录/call-axis",
  "depths_mm": [-40, -20, 0, 20],
  "tolerance_mm": 0.00001
}
```

平面为 `(P - O) · axis = depth_mm`，法向平行中心轴。
depth 是以轴原点为零点的有符号轴向距离，不是全局 Z 坐标。
最多64层；采样位置需要显式提供，没有默认层数或自动角度结论。
完整横截面保留轴两侧全部轮廓，不使用现有轴向截面的半平面筛选规则。

命令行：

```powershell
python -m rim_reconstruction.scripts.transverse_sections --axis-call-directory E:/实际调用目录/call-axis --depths -40 -20 0 20
```

独立几何 API（无需 FreeCAD、C++ 可执行程序或配置）：

```python
from rim_reconstruction.geometry.transverse_sections import load_stl, section_triangles
triangles = load_stl("E:/实际模型.stl")
result = section_triangles(triangles, origin=[0, 0, 0],
                           axis_direction=[0, 0, 1], depths_mm=[-10, 0, 10])
```

这里坐标仅为 API 示例；Agent 工具不接受编造的轴坐标。
支持二进制和ASCII STL。只依赖已有 NumPy，无需新增网格库。

返回摘要包含各层轮廓数、闭合轮廓数、分叉节点数、共面三角形数及轴向范围。
完整产物写在轴调用目录下 `transverse/transverse_<uuid>/sections.json`，包含：

- 源STL的SHA256、轴线来源、深度及拼接容差；
- 每层局部原点、U/V基向量与法向；
- 全部轮廓的UV和XYZ点、闭合状态、闭合环的投影面积。

开放链与分叉仅标记为 `needs_review`，不会强制补线。
完全共面三角形只贡献外边，不输出内部三角剖分边。
多个闭合环暂不区分外环、孔洞或窗口；不能将各环面积直接相加作为材料面积。
`status=success` 表示计算完成；层级 `empty` 表示没有交线，不是窗口识别成功。
本工具尚不进行窗口分类、周期识别、曲面拟合、阵列或布尔运算。
