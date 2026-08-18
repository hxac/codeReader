# u8-l3 网格导出与显存优化实战

## 1. 本讲目标

 DreamCraft3D 训练的终点产物不是一个神经网络，而是一个**可以在任意 DCC 软件（MeshLab、Blender、Unity）里打开的三维网格资产**。本讲要回答两个问题：

1. **网格是怎么导出来的**——`mesh-exporter` 如何把 DMTet 几何 + `no-material` 外观网络，变成一份带贴图的 `obj + mtl + texture_kd.jpg`？我们会逐行精读 xatlas UV 展开、UV 空间光栅化、纹理烘焙、缝隙修补与 obj 落盘这五步。
2. **显存是怎么省下来的**——README Tips 里的降分辨率技巧为什么有效？`precision: 16-mixed` 与 `32` 在四个阶段如何取舍？代码里还有哪些不易察觉的省显存设计（`fix_geometry` 网格缓存、`chunk_batch` 分块查询、`cleanup()`）？

学完本讲，你应该能独立完成一次带纹理的网格导出、看懂 obj/mtl 文件的每一行，并能针对自己的显卡制定一套显存优化方案。

## 2. 前置知识

本讲假设你已完成 u5-l4（DMTet 与 nvdiffrast）和 u2-l4（trial 目录与导出命令）。在此基础上补充三个概念：

- **UV 展开（UV unwrapping）**：三维网格表面是连续曲面，贴图是二维图片。UV 展开就是给每个三角面分配一段二维坐标 \((u, v) \in [0,1]^2\)，把曲面"摊平"到贴图平面上，使每个像素都能反查到自己对应曲面上的哪个点。因为曲面一般不可无撕裂地展平，算法会把网格切成若干**图卡（chart）**分别摊平，再打包（pack）进同一张图集（atlas）——这正是 `xatlas` 这个库做的事。
- **烘焙（baking）**：DreamCraft3D 的颜色不在贴图里，而在一个 MLP 里（几何特征经 `no-material` 的 sigmoid 直接输出 RGB，见 u5-l5）。导出时必须把这个隐式函数"烤"成一张固定贴图：对贴图上每个像素，反查出它对应的三维表面点，再把该点喂给 MLP 取颜色。
- **光度显存的来源**：体渲染阶段（coarse）的激活值规模约为 \(H \times W \times N_{\text{samples}}\)（每条射线 512 个采样点），光栅化阶段（geometry/texture）约为 \(H \times W\)。所以把渲染分辨率从 256 降到 128，激活显存大约缩小到 \(1/4\)——这是降分辨率省显存的数学根源。相比之下，扩散引导模型（DeepFloyd 吃 64×64 像素图）的显存基本不随渲染分辨率变化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `threestudio/models/exporters/mesh_exporter.py` | 本讲主角：`mesh-exporter` 注册组件，UV 展开的调度与纹理烘焙全流程 |
| `threestudio/models/exporters/base.py` | `Exporter` 基类、`ExporterOutput` 数据类、`dummy-exporter` |
| `threestudio/systems/base.py` | `on_predict_start` / `on_predict_epoch_end`：导出器与 Lightning predict 循环的接线 |
| `threestudio/models/mesh.py` | `Mesh.unwrap_uv` / `_unwrap_uv`：xatlas 调用与顶点重编号 |
| `threestudio/utils/rasterize.py` | `NVDiffRasterizerContext`：`rasterize_one` / `interpolate_one` 封装 |
| `threestudio/models/geometry/tetrahedra_sdf_grid.py` | `isosurface()`（含 `fix_geometry` 缓存）与 `export()`（外观特征查询） |
| `threestudio/models/materials/no_material.py` | `export()`：特征 → albedo 颜色 |
| `threestudio/utils/saving.py` | `save_obj` / `_save_obj` / `_save_mtl`：obj+mtl 文本格式落盘 |
| `threestudio/utils/misc.py` | `cleanup()`：gc + `empty_cache` + tiny-cuda-nn 临时内存释放 |
| `configs/dreamcraft3d-*.yaml` | 四阶段分辨率、`precision`、`fix_geometry` 等显存相关配置 |
| `README.md` | Tips 一节的官方降分辨率技巧与导出命令 |

## 4. 核心概念与源码讲解

### 4.1 导出器插件：Exporter 基类与 mesh-exporter 的入口接线

#### 4.1.1 概念说明

导出器（exporter）是 threestudio 插件体系里与 geometry/renderer/guidance 平级的一类可替换组件（注册名 `mesh-exporter`）。它的特殊之处在于：

- 它**不拥有任何权重**。`configure` 时系统把**自己正在用的** geometry/material/background 三个实例直接传进来共享，导出器只是借用它们做一次推理。
- 它**不参与训练**。只有 `--export` 模式（Lightning 的 `trainer.predict` 路径）才会把它构建出来，用完即弃。

#### 4.1.2 核心流程

```text
launch.py --export
  └─ set_system_status(system, cfg.resume)     # 从 ckpt 恢复 epoch/global_step
  └─ trainer.predict(system, ckpt_path=resume)
       ├─ Lightning 加载 ckpt 权重 → system
       ├─ BaseSystem.on_predict_start()
       │    └─ find(exporter_type)(cfg.exporter, geometry=…, material=…, background=…)
       ├─ predict_step()                        # save_video=False 时是空操作
       └─ BaseSystem.on_predict_epoch_end()
            └─ outputs = exporter()             # 真正干活：返回 [ExporterOutput]
                 └─ save_func = "save_" + save_type  → self.save_obj(...)
                      保存到 save/it{true_global_step}-export/model.obj
```

#### 4.1.3 源码精读

先看基类。`ExporterOutput` 只是一个"待保存清单"：文件名、类型（决定调用 `save_obj` 还是其他 `save_*`）、以及透传给保存函数的参数包：

- [threestudio/models/exporters/base.py:11-15](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L11-L15) 定义 `ExporterOutput(save_name, save_type, params)` 三个字段。
- [threestudio/models/exporters/base.py:25-37](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L25-L37) `Exporter.configure` 用一个局部 `SubModules` dataclass 把系统传入的三个组件挂到 `self.sub_modules` 上——注意是**赋引用**而非深拷贝，所以导出器看到的就是系统里那份训练好的几何与材质。
- [threestudio/models/exporters/base.py:39-49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L39-L49) 用 `@property` 把 `sub_modules.geometry` 等暴露成 `self.geometry` / `self.material` / `self.background`，让子类写起来像直接持有它们。
- [threestudio/models/exporters/base.py:55-59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L55-L59) 还注册了一个 `dummy-exporter`，`__call__` 返回空列表——占位用，说明"不导出任何东西"也是插件的一种合法实现。

系统侧的接线在 `BaseLift3DSystem`：

- [threestudio/systems/base.py:237-239](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L237-L239) `exporter_type` 的默认值就是 `"mesh-exporter"`，所以 README 导出命令里那句 `system.exporter_type=mesh-exporter` 其实是显式重申默认值（对老 trial 的 parsed.yaml 起兜底作用）。
- [threestudio/systems/base.py:311-317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L311-L317) `on_predict_start` 在 predict 循环开始前，用注册机制 `find(exporter_type)` 构建导出器并注入共享组件。
- [threestudio/systems/base.py:319-321](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L319-L321) `predict_step` 只有当 `exporter.cfg.save_video=True`（默认 False）才会调 `test_step` 渲染视频，默认导出是纯网格导出、不渲染。
- [threestudio/systems/base.py:323-332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L323-L332) `on_predict_epoch_end` 是导出的真正触发点：调用 `self.exporter()` 拿到输出列表，再按 `save_type` 反射出 `save_obj` 方法，存到 `save/it{true_global_step}-export/` 下。

再看 mesh-exporter 自己的配置与入口：

- [threestudio/models/exporters/mesh_exporter.py:17-30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L17-L30) `MeshExporter.Config`：`fmt` 支持 `obj-mtl`（带材质贴图，默认）与 `obj`（顶点色）；`save_normal` 默认关；`save_uv` / `save_texture` 默认开；`texture_size=1024` 决定贴图边长；`texture_format=jpg`；`xatlas_chart_options` / `xatlas_pack_options` 两个 dict 透传给 xatlas；`context_type` 默认 `"gl"`（注意与 geometry/texture 训练配置里渲染器的 `"cuda"` 不同）。
- [threestudio/models/exporters/mesh_exporter.py:34-41](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L34-L41) `configure` 额外创建一个**独立的** `NVDiffRasterizerContext`——u2-l4 已指出导出器与渲染器各持一份上下文，互不干扰。
- [threestudio/models/exporters/mesh_exporter.py:43-51](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L43-L51) `__call__` 只做两件事：向几何要网格（`self.geometry.isosurface()`），再按 `fmt` 分发给 `export_obj_with_mtl` 或 `export_obj`。

两个工程细节值得强调：

- [launch.py:208-210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L208-L210) `--export` 分支先 `set_system_status` 手动把 ckpt 里的 `epoch/global_step` 灌回 system，再 `trainer.predict`。没有这一步，`true_global_step` 为 0，导出目录会变成 `it0-export`（u2-l4 的结论，这里是它的代码落点）。
- [launch.py:179-186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L179-L186) `Trainer(inference_mode=False)`：Lightning 默认用 `torch.inference_mode()` 包住验证/预测前向，而这与 nvdiffrast 这类 CUDA 扩展的梯度图不兼容，所以必须关掉——这就是导出路径也能走可微光栅化的原因。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次导出，并确认调用链上的每个环节都如期发生。

**操作步骤**（需要有训练完成的 ckpt；若暂无，可先用短步数跑通 texture 阶段，或只做第 3 步的源码验证）：

1. 用 README 的命令对 texture 阶段 ckpt 导出（`<trial>` 替换为自己的 trial 目录）：

   ```sh
   python launch.py --config <trial>/configs/parsed.yaml --export --gpu 0 \
     resume=<trial>/ckpts/last.ckpt system.exporter_type=mesh-exporter
   ```

2. 观察终端日志，应依次出现 `"Exporting textures ..."`、`"Perform UV padding on texture maps ..."` 与 `"Export assets saved to ..."` 三条信息（前两条来自 4.3 将精读的代码，第三条来自 [threestudio/systems/base.py:334-336](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L334-L336)）。
3. （无 GPU 也可做）源码验证：在 `on_predict_start` 的 `self.exporter = ...` 一行上下加打印（本地临时修改，验完还原），或直接确认 `find("mesh-exporter")` 返回 `MeshExporter` 类——这验证注册机制的消费端。

**需要观察的现象**：导出目录 `<trial>/save/it{N}-export/` 下生成 `model.obj`、`model.mtl`、`texture_kd.jpg` 三个文件，且目录名中的 N 等于训练总步数（如 5000）而非 0。

**预期结果**：N>0 证明 `set_resume_status` 生效；三个文件齐全证明走的是 `obj-mtl` 分支。若 N=0，检查 `resume=` 是否漏传。本实践需要真实 ckpt，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把导出命令里的 `system.exporter_type` 改成 `dummy-exporter`，会发生什么？
**答案**：`find("dummy-exporter")` 返回 `DummyExporter`，其 `__call__` 返回空列表，`on_predict_epoch_end` 的 for 循环零次迭代，最终只在 `save/it{N}-export/` 下什么也不生成（目录可能都不会有文件）。这可以用来验证导出链路本身是否通畅。

**练习 2**：为什么导出器能直接用 `self.geometry`，而这个 geometry 里存的是训练好的权重？
**答案**：`on_predict_start` 构建 exporter 时传入的是 system 已有的三个组件实例（引用共享），而 system 的权重又来自 `trainer.predict(..., ckpt_path=cfg.resume)` 的 ckpt 加载。所以 exporter 看到的 geometry 就是"加载了 last.ckpt 的 geometry"，无需二次读盘。

**练习 3**：`predict_step` 默认是空操作，那 `trainer.predict` 这一趟到底为了什么？
**答案**：为了借道 Lightning 的 predict 生命周期钩子：`on_predict_start`（构建 exporter）与 `on_predict_epoch_end`（执行导出与保存）都只在 predict 循环里被调用。数据本身无关紧要，重要的是这两个钩子的触发时机（u2-l4 结论的复述与代码确认）。

### 4.2 xatlas UV 展开：从三维网格到二维图集

#### 4.2.1 概念说明

`Mesh` 对象天生只有位置拓扑（`v_pos` + `t_pos_idx`），没有 UV。`unwrap_uv` 调用 `xatlas` 库完成三件事：

1. **分卡（charting）**：按曲面曲率把网格切成若干近似可展平的区域；
2. **参数化**：把每张卡摊平到平面，得到每个顶点的 \((u,v)\)；
3. **打包（packing）**：把所有卡紧凑排进 \([0,1]^2\) 的图集里，卡与卡之间留 `padding` 像素的隔离带。

关键副作用：为了让被切开的顶点各自拥有不同的 UV，xatlas 会**拆分并重编号顶点**，返回一套新的顶点表与面表。新网格的第 k 个面与原网格的第 k 个面是**同一个三角形**，只是顶点编号换了一套。

#### 4.2.2 核心流程

```text
mesh_exporter.export_obj_with_mtl
  └─ mesh.unwrap_uv(chart_options, pack_options)
       └─ Mesh._unwrap_uv
            ├─ xatlas.Atlas(); add_mesh(v_pos, t_pos_idx)
            ├─ 把两个 options dict 逐键 setattr 到 ChartOptions/PackOptions
            ├─ atlas.generate(co, po)
            └─ 取回 (vmapping, indices, uvs)，返回 (uvs, t_tex_idx)
此后：
  mesh.v_tex    → 新 UV 顶点坐标 [Nt, 3]（第三维无用）
  mesh.t_tex_idx→ UV 拓扑的面表 [Nf, 3]（与 t_pos_idx 同面数、异顶点编号）
```

#### 4.2.3 源码精读

- [threestudio/models/mesh_exporter.py:69-70](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L69-L70) 导出入口处按需调用 `mesh.unwrap_uv`，两个 options 直接来自 Config 的 dict，缺省为空 dict（即全用 xatlas 默认参数）。
- [threestudio/models/mesh.py:207-218](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L207-L218) `_unwrap_uv` 先把顶点/面转成 numpy 喂给 `xatlas.Atlas`——注意 `v_pos.detach().cpu()`，UV 展开完全在 CPU、无梯度空间进行（展开是离散决策，本就不可微，与 u5-l4"离散不求导"的原则一致）。
- [threestudio/models/mesh.py:219-225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L219-L225) 用 `setattr` 把 yaml 里的 `xatlas_chart_options` / `xatlas_pack_options` 逐键打到 xatlas 的选项对象上——这是一种"不改代码就能透传任意 xatlas 参数"的松散接口。
- [threestudio/models/mesh.py:226-242](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L226-L242) 取回三件套：`vmapping`（新顶点 → 原顶点的映射表）、`indices`（新面表）、`uvs`（新顶点坐标）。仔细看 `return uvs, indices`——**`vmapping` 被构造却没有被返回**，它是本函数里的死代码。后续烘焙能正常工作，靠的是"同面双编号"这一对应关系（见 4.3），而非顶点映射表。
- [threestudio/models/mesh.py:244-249](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L244-L249) `unwrap_uv` 把结果写进 `self._v_tex` / `self._t_tex_idx` 缓存；而 [threestudio/models/mesh.py:113-122](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/mesh.py#L113-L122) 的 `v_tex` / `t_tex_idx` property 保证按需懒展开（不带参数、用默认 options）。

#### 4.2.4 代码实践

**实践目标**：直观感受"xatlas 会拆分顶点、改变顶点数量，但保持面的一一对应"。

**操作步骤**：

1. 写一个独立小脚本（**示例代码**，非项目原有）：

   ```python
   import torch, xatlas, trimesh

   mesh = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
   v, f = mesh.vertices, mesh.faces

   atlas = xatlas.Atlas()
   atlas.add_mesh(v, f)
   atlas.generate(xatlas.ChartOptions(), xatlas.PackOptions())
   vmapping, indices, uvs = atlas.get_mesh(0)

   print("原顶点数:", len(v), " 新顶点数:", len(uvs))
   print("原面数:", len(f), " 新面数:", len(indices))
   ```

2. 运行并对比两组数字；再换成一张真实的 DMTet 导出网格（可用 4.1 实践得到的 `model.obj`，用 `trimesh.load` 读入）重复一次。

**需要观察的现象**：新顶点数明显多于原顶点数（球缝处顶点被复制），而面数不变。

**预期结果**：顶点数上涨、面数持平。具体数值**待本地验证**（不同网格、不同 xatlas 版本结果会有差异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 xatlas 必须拆分顶点？不拆行不行？
**答案**：UV 展开要把网格切开摊平，切缝两侧的同一个顶点会落到图集的不同位置，需要两份不同的 \((u,v)\)，因此必须复制顶点。不拆分就无法表达切缝，图集会重叠拉伸。

**练习 2**：`_unwrap_uv` 里构造的 `vmapping` 没有被返回，这算 bug 吗？
**答案**：对当前导出流程而言不是 bug——烘焙与写 obj 只依赖"同面双编号"（`t_tex_idx[k]` 与 `t_pos_idx[k]` 指同一三角形），不需要新顶点到原顶点的反查表。但它是一处容易误导读者的死代码；若未来要在 UV 顶点上搬运逐顶点属性（如顶点色），就需要它。

**练习 3**：想在 yaml 里控制图集打包的留白宽度，应该配哪个键？
**答案**：`system.exporter.xatlas_pack_options` 下的 `padding` 键（如 `system.exporter.xatlas_pack_options.padding=4`）。它不仅影响 xatlas 打包，还被 4.3 的 `uv_padding` 读取作为 inpaint 半径（[threestudio/models/exporters/mesh_exporter.py:93-94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L93-L94) 的 `get("padding", 2)`）。

### 4.3 UV 空间光栅化与纹理烘焙

#### 4.3.1 概念说明

这是本讲最精彩的一段：**把贴图平面当作一个"屏幕"，把 UV 网格当作一个"场景"，用 nvdiffrast 光栅化它**。光栅化输出 `rast` 的每个像素记录了"这个像素落在哪个三角形上、重心坐标是多少"；用这份重心坐标去插值三维坐标 `v_pos`，就得到"贴图像素 → 三维表面点"的反查表 `gb_pos`；再把每个表面点喂给几何编码 + 外观 MLP，颜色就"烤"进了贴图。训练时渲染是"三维 → 屏幕"，烘焙是反方向的"贴图 → 三维"，用的却是同一套光栅化原语。

#### 4.3.2 核心流程

```text
uv_clip  = v_tex * 2 - 1                    # [0,1] → [-1,1] 裁剪空间
uv_clip4 = (u, v, 0, 1)                     # 补齐四分量齐次坐标
rast     = ctx.rasterize_one(uv_clip4, t_tex_idx, (1024, 1024))
hole_mask= ~(rast[:,:,3] > 0)               # 图卡之间的缝隙像素
gb_pos   = ctx.interpolate_one(v_pos, rast, t_pos_idx)   # 逐像素三维坐标（重心插值）
geo_out  = geometry.export(points=gb_pos)   # 哈希编码 + MLP → features
mat_out  = material.export(points=gb_pos, **geo_out)     # sigmoid → albedo
map_Kd   = uv_padding(albedo)               # cv2.inpaint 补缝
```

其中 UV → 裁剪空间的变换是逐分量的

\[
u_{\text{clip}} = 2u - 1, \qquad v_{\text{clip}} = 2v - 1,
\]

重心坐标插值则是

\[
\mathbf{p}_{\text{pixel}} = \lambda_0 \mathbf{p}_0 + \lambda_1 \mathbf{p}_1 + \lambda_2 \mathbf{p}_2,
\qquad \lambda_0+\lambda_1+\lambda_2 = 1,
\]

\(\lambda_i\) 由 `rast` 直接给出，\(\mathbf{p}_i\) 取自 `v_pos[t_pos_idx[k]]`（k 为该像素覆盖的面编号）。

#### 4.3.3 源码精读

- [threestudio/models/exporters/mesh_exporter.py:72-85](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L72-L85) 先断言 `save_texture` 必须依赖 `save_uv`；随后做 UV→裁剪空间变换并补零/补一成四分量。`rast` 的分辨率就是 `texture_size=1024`。
- [threestudio/models/exporters/mesh_exporter.py:87-91](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L87-L91) 用 `rasterize_one` 光栅化 UV 网格；`hole_mask` 取 alpha 通道（第 4 分量）的否定——凡是没被任何三角形盖住的像素（图卡之间的隔离带、抗锯齿缝隙）都是"洞"。
- [threestudio/models/exporters/mesh_exporter.py:106-110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L106-L110) `interpolate_one(mesh.v_pos, rast, mesh.t_pos_idx)`：注意这里**故意混用两套拓扑**——`rast` 里的面编号来自 UV 拓扑的光栅化，查顶点时却用位置拓扑的 `t_pos_idx`。这能成立，正是因为 4.2 说的"同面双编号"：第 k 个面在两套编号下是同一个三角形，UV 空间算出的重心坐标可以直接拿来插值它的三个三维顶点。
- [threestudio/models/exporters/mesh_exporter.py:112-114](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L112-L114) 烘焙核心两行：`geometry.export(points=gb_pos)` 出特征，`material.export(points=gb_pos, **geo_out)` 出颜色。
- [threestudio/models/geometry/tetrahedra_sdf_grid.py:354-369](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L354-L369) `TetrahedraSDFGrid.export`：世界坐标先 `contract_to_unisphere` 归一化到 (0,1)，过 `ProgressiveBandHashGrid`（此处直接用 `HashGrid` 配置）编码与 `feature_network`，返回 `{"features": ...}`。它与 `forward` 的区别只有一点：导出时不需要法向，且被当成纯推理函数调用。
- [threestudio/models/materials/no_material.py:56-63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/materials/no_material.py#L56-L63) `NoMaterial.export` 复用 `forward`（sigmoid 激活）再 `clamp(0,1)`，返回 `{"albedo": color}`——u5-l5 说过 `export` 与 `forward` 同源，所以**训练时被监督的颜色和导出时被烘焙的颜色是同一个函数**，不存在"训练好看、导出变色"的缝隙。
- [threestudio/models/exporters/mesh_exporter.py:93-104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L93-L104) `uv_padding` 闭包：把张量转成 uint8 图与洞掩码，用 OpenCV 的 `cv2.inpaint(..., cv2.INPAINT_TELEA)` 按周围像素插值填洞，半径取 `xatlas_pack_options` 的 `padding`（默认 2）。填洞是给下游查看器的双线性过滤/mipmap 留余量：采样点落到缝隙附近时不至于采到黑边。
- [threestudio/models/exporters/mesh_exporter.py:116-131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L116-L131) 按 `mat_out` 里有哪些键决定烘几张图：DreamCraft3D 用 `no-material`，只有 `albedo` → 只有 `map_Kd`；若换成 PBR 材质，`metallic/roughness/bump` 会各自烘一张（本仓库未启用）。
- 底层封装见 [threestudio/utils/rasterize.py:39-47](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L39-L47)（`rasterize_one`：单网格单视角）与 [threestudio/utils/rasterize.py:70-78](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/rasterize.py#L70-L78)（`interpolate_one`：属性插值）。

顺带一提 `fmt="obj"` 的轻量分支：[threestudio/models/exporters/mesh_exporter.py:158-169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L158-L169) 不做 UV 光栅化，直接在**顶点**上查 `geometry.export(mesh.v_pos)`，把颜色写成顶点色（`set_vertex_color`）。顶点色精度受顶点密度限制，所以默认走的是 `obj-mtl` 贴图分支。

#### 4.3.4 代码实践

**实践目标**：体会 `texture_size` 对贴图质量与文件体积的影响，并掌握用命令行覆盖导出参数（不改源码、不改 yaml）。

**操作步骤**：

1. 在 4.1 的导出命令末尾追加一个覆盖：

   ```sh
   python launch.py --config <trial>/configs/parsed.yaml --export --gpu 0 \
     resume=<trial>/ckpts/last.ckpt system.exporter_type=mesh-exporter \
     system.exporter.texture_size=512
   ```

2. 导出后把产物挪到别的目录，再用默认 `texture_size=1024` 导出一次。
3. 并排对比两张 `texture_kd.jpg`：放大看图卡边缘的细节（如纹理花纹、文字）哪个更糊；记录两张 jpg 的文件大小与像素尺寸。

**需要观察的现象**：512 版贴图面积为 1024 版的 \(1/4\)，文件大小显著缩小，细部（高频纹理）明显模糊；整体色块与低频信息一致（因为查询的是同一个 MLP）。

**预期结果**：贴图边长与 `texture_size` 一致；细节损失集中在高频。若想连图集布局一起变，还需调 `xatlas_pack_options`（如 `resolution` 相关参数，**待确认**具体键名，可查 xatlas 文档）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：烘焙时为什么不直接遍历网格顶点算颜色（像 `fmt="obj"` 那样），而要费力做 UV 光栅化？
**答案**：顶点数有限（DMTet 网格顶点密度不均），顶点色在查看器里靠插值呈现，高频纹理会糊；UV 光栅化在 1024×1024 ≈ 百万像素上密集采样 MLP，贴图细节远超顶点密度。代价是要先解决"像素→表面点"的反查，而这恰好可以用光栅化优雅地完成。

**练习 2**：`hole_mask` 里的洞是怎么产生的？不补会怎样？
**答案**：来自图卡之间预留的隔离带、三角形抗锯齿边缘与打包空隙。下游渲染器对贴图做双线性过滤或 mipmap 时会采样到这些洞，若不补（保持未初始化值/黑色）就会出现接缝黑边；`cv2.inpaint` 用邻域颜色外推填洞，牺牲少量精度换无缝。

**练习 3**：`geometry.export` 里 `contract_to_unisphere` 用的 `self.bbox` 与 `isosurface()` 用的 `self.isosurface_bbox` 是一回事吗？
**答案**：不是。`bbox` 是 configure 时由 `radius` 生成的固定立方体（半径 2.0 的包围盒 buffer），`export` 把世界坐标归一到这个盒子；而 `isosurface_bbox` 是 create_from 时从上一阶段继承的紧凑包围盒（[threestudio/models/geometry/tetrahedra_sdf_grid.py:69-75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L69-L75)），只用于等值面提取时的坐标缩放。训练时的 `forward` 与导出时的 `export` 用的是同一个 `bbox`，所以两边一致。

### 4.4 obj+mtl 落盘：save_obj 的文件格式细节

#### 4.4.1 概念说明

`mesh-exporter` 自己不写文件，它返回的 `ExporterOutput.params` 会被 `on_predict_epoch_end` 透传给 `SaverMixin.save_obj`。obj 是纯文本格式，理解它的每类行（`v/vn/vt/f/mtllib/usemtl`）与 mtl 的 `map_Kd` 引用，是检查导出产物、与其他工具链对接的基本功。

#### 4.4.2 核心流程

```text
on_predict_epoch_end
  └─ self.save_obj(f"it{N}-export/model.obj", **out.params)
       ├─ save_mat=True → _save_mtl("model.mtl", "default", map_Kd=贴图)
       │     ├─ 写 newmtl/Ka/map_Kd texture_kd.jpg
       │     └─ _save_rgb_image 把张量存成 texture_kd.jpg
       └─ _save_obj("model.obj")
             ├─ mtllib model.mtl / usemtl default
             ├─ v  x y z [r g b]
             ├─ vn x y z            (save_normal 时)
             ├─ vt u (1-v)          ← 注意 y 翻转
             └─ f  v/vt/vn ...      ← 1-based 索引
```

#### 4.4.3 源码精读

- [threestudio/utils/saving.py:441-499](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L441-L499) `save_obj` 入口：按开关拆出 `v_nrm/v_tex/t_tex_idx/v_rgb`，`save_mat=True` 时同步生成 mtl 并把贴图一并写盘，最后拼 obj 文本。
- [threestudio/utils/saving.py:513-528](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L513-L528) obj 文本逐行拼装：`mtllib`/`usemtl` 头（L514-517）、顶点 `v`（可选拼顶点色，L518-522）、法向 `vn`（L523-525）、纹理坐标 `vt`（L526-528）。注意 L528 的 `vt {v[0]} {1.0 - v[1]}`：**v 坐标做了 \(v' = 1 - v\) 翻转**。原因是 OBJ 约定 vt 原点在左下角，而光栅化烘焙出的贴图与 OpenCV 存图都以左上角为原点；这一翻转与烘焙时的坐标链路相互抵消，最终在查看器里方向才是正的。
- [threestudio/utils/saving.py:530-539](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L530-L539) 面行 `f` 的三段式索引 `顶点/纹理/法向`，且全部 `+1`——OBJ 索引从 1 开始，而张量下标从 0 开始。
- [threestudio/utils/saving.py:546-580](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L546-L580) `_save_mtl`：写 `newmtl default`、环境光 `Ka`；有 `map_Kd` 就写 `map_Kd texture_kd.jpg` 并把贴图存到 mtl 同目录（L565-578），没有则退回常数 `Kd`。`map_Ks/map_Bump/map_Pm/map_Pr` 同理对应高光/凹凸/金属度/粗糙度贴图——DreamCraft3D 只产 `map_Kd`。
- 最终产物三件套落在 `save/it{N}-export/` 下：`model.obj`、`model.mtl`、`texture_kd.jpg`，mtl 与贴图用相对路径互相引用，拷贝时三件必须同去。

#### 4.4.4 代码实践

**实践目标**：用肉眼验证 obj/mtl 文本格式与代码逐行对应。

**操作步骤**（不需要 GPU，有导出产物即可；没有产物可用任何带 UV 的 obj 做替代练习）：

1. 查看文件头与各类行（**示例命令**）：

   ```sh
   cd <trial>/save/it*-export
   head -n 5 model.obj          # 应看到 mtllib/usemtl 与第一行 v
   grep -c "^v "  model.obj      # 顶点数
   grep -c "^vt " model.obj      # UV 顶点数（应大于 v 数：xatlas 拆分所致）
   grep -c "^f "  model.obj      # 面数
   cat model.mtl                 # 应有 newmtl default 与 map_Kd texture_kd.jpg
   ```

2. 检查第一行 `vt` 的第二个分量：确认它落在 \([0,1]\) 内（它是 \(1-v\) 的结果）。
3. 用 MeshLab 或 Blender 打开 `model.obj`，切到纹理显示模式，环绕观察有无明显接缝黑边（检验 4.3 的 inpaint 效果）。

**需要观察的现象**：`vt` 行数 > `v` 行数；`f` 行索引形如 `a/b/c`；mtl 引用的 `texture_kd.jpg` 与 obj 同目录。

**预期结果**：数字关系与 4.2 的"xatlas 拆顶点、保面数"结论互相印证；MeshLab 中纹理无黑缝。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `f` 行索引要 `+1`，`vt` 的 v 要翻转，而 `v`/`vn` 不用任何变换？
**答案**：`+1` 是 OBJ 的 1-based 索引约定；vt 翻转是 OBJ 左下原点与图像左上原点之别；`v` 存的是世界坐标（与 bbox 缩放后的真实尺度），`vn` 是单位向量，都不涉及坐标系镜像约定。

**练习 2**：把 `model.mtl` 单独拷走会发生什么？
**答案**：obj 里的 `mtllib model.mtl` 找不到材质库，查看器退回默认灰模；同理丢掉 `texture_kd.jpg` 则 `map_Kd` 失效。三件套必须同目录一起分发。

**练习 3**：想让导出附带法向（`vn` 行），改哪里？
**答案**：命令行覆盖 `system.exporter.save_normal=true` 即可（对应 [threestudio/models/exporters/mesh_exporter.py:23](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/mesh_exporter.py#L23) 的开关），`save_obj` 会经 `mesh.v_nrm`（面法向面积加权平均，见 u5-l4）写出 `vn`。

### 4.5 显存优化手段在代码中的落点

#### 4.5.1 概念说明

README 对显存只给了一句 Tips，但代码里的省显存设计散布在配置、几何、导出与工具层。把它们收拢成一张清单，遇到 OOM 时就能按"影响面从小到大"逐项尝试：

| 手段 | 位置 | 作用面 |
| --- | --- | --- |
| 降渲染分辨率（官方 Tips） | yaml `data.height/width` 与 `data.random_camera.height/width` | 体渲染/光栅化激活值，约 \( \propto H W \) |
| 混合精度 `16-mixed` | yaml `trainer.precision` | 全部激活与部分权重 |
| `fix_geometry` 网格缓存 | `tetrahedra_sdf_grid.isosurface()` | texture 阶段重复的等值面提取 |
| `chunk_batch` 分块查询 | 隐式几何 `_isosurface` | 等值面提取的峰值 |
| `cleanup()` | `misc.py`，阶段切换/评估后调用 | 碎片与缓存池 |
| xformers / 梯度检查点 | 辅助 DreamBooth 脚本 CLI 开关 | 扩散模型 UNet 显存 |

#### 4.5.2 核心流程

以"coarse-neus 阶段 256 → 128"为例，降分辨率的作用路径：

```text
data.height=128 data.width=128
data.random_camera.height=128 data.random_camera.width=128
  └─ 每条批次的射线数从 256×256 降到 128×128（1/4）
       └─ nerfacc ray marching 采样点数 → 1/4
            └─ 编码/MLP/合成中间张量 → 约 1/4
扩散引导（DeepFloyd 64×64 输入、Zero123 256×256 潜空间）→ 基本不变
```

#### 4.5.3 源码精读

- **官方 Tips**：[README.md:168-169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L168-L169) 明确给出 NeuS 阶段的降分辨率写法，并说明"其他阶段同理"。注意要点是**主 `data` 与子数据集 `data.random_camera` 两处都要覆盖**——u4-l2 讲过训练 batch 是双层嵌套，随机相机子 batch 的分辨率由后者单独控制；另外参考视角的三件套图会按该档分辨率重读，监督信号同步缩小。
- **精度取舍**：四份配置的 `trainer.precision` 分别是——coarse-nerf [configs/dreamcraft3d-coarse-nerf.yaml:154](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L154) 为 `16-mixed`，coarse-neus/geometry/texture 均为 `32`（geometry 见 [configs/dreamcraft3d-geometry.yaml:127](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-geometry.yaml#L127)，texture 见 [configs/dreamcraft3d-texture.yaml:160](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L160)）。取舍逻辑可从各阶段要优化的对象读出：coarse 阶段优化的是连续密度场，梯度量级温和，混合精度的收益（激活减半）大于风险；geometry/texture 阶段直接以全精度优化网格顶点上的 sdf/deformation 参数（u5-l4）与 BSD 的两个 UNet（u7-l5），这些参数对数值误差敏感，官方选择保守的 fp32。这是一个可以自行实验的开关（`trainer.precision=16-mixed` 可经命令行覆盖），但**偏离默认配置的效果待本地验证**。
- **`fix_geometry` 网格缓存**：[threestudio/models/geometry/tetrahedra_sdf_grid.py:237-248](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L237-L248) `isosurface()` 开头的短路分支——texture 阶段 `fix_geometry: true`（[configs/dreamcraft3d-texture.yaml:59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59)），几何冻结则网格永不改变，第一次提取后缓存复用，后续每个渲染步都省掉一次 marching tetrahedra（显存与时间双赢）。导出时 `__call__` 里的 `geometry.isosurface()` 同样吃到这份缓存。
- **`chunk_batch` 分块**：隐式几何做等值面提取时要对**全部网格顶点**前向算场值，一次性算完峰值很大；[threestudio/models/geometry/base.py:136-140](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L136-L140) 用 `chunk_batch` 按 `isosurface_chunk` 分批（默认 0 表示不分批，可按需调小）。
- **`cleanup()`**：[threestudio/utils/misc.py:100-103](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L100-L103) 三连：Python gc、`torch.cuda.empty_cache()`、`tcnn.free_temporary_memory()`（释放 tiny-cuda-nn 的临时显存池——它不受 PyTorch 缓存分配器管理，`empty_cache` 够不着它）。调用点包括阶段切换删除旧几何后（[threestudio/systems/base.py:284](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L284)）与可选的评估后清理（`cleanup_after_validation_step` 开关，[threestudio/systems/base.py:127-129](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L127-L129)）。
- **分辨率课程**本身也是一种显存调度：coarse-nerf 配置 [configs/dreamcraft3d-coarse-nerf.yaml:9-11](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L9-L11) 的 `height: [128, 384]` + `resolution_milestones: [3000]` 表示前 3000 步只渲染 128×128、之后翻到 384——几何未成型时用低分辨率快速迭代，把高显存开销推迟到最需要的阶段（机制细节见 u3-l2 / u4-l1）。
- **xformers 与梯度检查点**：这两个 diffusers 生态的常规武器在本仓库**只出现在辅助 DreamBooth 训练脚本里**，且是命令行开关而非 yaml 配置——[threestudio/scripts/train_dreambooth_lora.py:851-862](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/scripts/train_dreambooth_lora.py#L851-L862)（`--enable_xformers_memory_efficient_attention` 开启 UNet 的 xformers 注意力）与 [threestudio/scripts/train_dreambooth_lora.py:864-867](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/scripts/train_dreambooth_lora.py#L864-L867)（`--gradient_checkpointing` 以重算换显存）。主训练入口 `launch.py` 的路径没有暴露这两个开关；README 的官方建议就是降分辨率（[README.md:54](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L54) 给出的门槛是 20GB 显存，默认配置在 40G A100 上开发）。

#### 4.5.4 代码实践

**实践目标**：把散落的显存相关配置收拢成自查清单，为综合实践的实测做准备。

**操作步骤**：

1. 在四份 `configs/dreamcraft3d-*.yaml` 中检索以下键并填表：`data.height`、`data.width`、`data.random_camera.height`、`data.random_camera.width`、`resolution_milestones`、`trainer.precision`、`system.geometry.fix_geometry`（如有）、`system.geometry.isosurface_resolution`。
2. 对照 [README.md:168-169](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L168-L169)，为每个阶段写出"降到 128 需要覆盖哪几个键"。
3. （可选实验）粗跑 100 步比较 `trainer.precision=32` 与默认 `16-mixed` 的 coarse-nerf 显存占用。

**需要观察的现象**：表格应呈现出 u2-l3 已总结的"分辨率 128→256→1024 爬坡、精度 16-mixed→32"的课程式安排；texture 阶段 `fix_geometry: true` 与 `isosurface_remove_outliers: true` 同时出现。

**预期结果**：得到一张四阶段显存配置对照表。第 3 步的量化结论**待本地验证**（一般混合精度可省约三至五成激活显存，但具体数值依赖硬件）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 README 的 Tips 要求同时覆盖 `data.height` 与 `data.random_camera.height`，只改一个会怎样？
**答案**：参考视角子步用主 `data` 的分辨率加载三件套并渲染，guidance 子步用 `random_camera` 子数据集的分辨率。只改一个会造成两个子步分辨率不匹配——监督图与渲染图尺寸对不上（参考子步报错），或监督图先被下采样再与高分辨率随机渲染混训（损失权重失衡）。

**练习 2**：`fix_geometry` 的网格缓存为什么不会导致"导出旧几何"的错误？
**答案**：缓存只在几何冻结（参数不再是 Parameter 而是 buffer，梯度无从更新）时启用；几何既然不会变，缓存与重算结果一致。反过来若 `fix_geometry=false`（geometry 阶段），每次 `isosurface()` 都重新 marching tetrahedra，保证拿到最新形状。

**练习 3**：已经 OOM 了，按什么顺序尝试？（结合本讲清单）
**答案**：① 降 `data` 与 `data.random_camera` 分辨率（影响面最大、官方推荐）；② 确认所在阶段是否本该用 `16-mixed`（coarse 默认已是）；③ 调小 `system.geometry.isosurface_chunk` 或渲染器的 `num_samples_per_ray`（u5-l2 讨论过后者）；④ 若在跑 DreamBooth 个性化脚本，加 `--gradient_checkpointing` 与 xformers 开关；⑤ 仍不行则换更大显存卡——BSD 双 UNet 全参训练的底线性开销（u7-l4）不是配置能消掉的。

## 5. 综合实践

**任务**：完成一次完整的"导出 + 显存对比"闭环，产出一份自己写的分析。分两部分：

### 第一部分：导出 obj+mtl 并检查贴图

1. （前置）按 u1-l2 搭好环境，用 README Quickstart 跑完四阶段（显存不足可全程按 4.5 的 Tips 降分辨率，步数可用 `trainer.max_steps=500` 缩短，仅为本实践产出可用 ckpt，质量不作要求）。
2. 对 texture 阶段 ckpt 执行导出：

   ```sh
   trial=outputs/dreamcraft3d-texture/<你的tag>@<时间戳>
   python launch.py --config $trial/configs/parsed.yaml --export --gpu 0 \
     resume=$trial/ckpts/last.ckpt system.exporter_type=mesh-exporter
   ```

3. 按 4.4.4 的命令检查 `model.obj` / `model.mtl` / `texture_kd.jpg`：数一数 `v` 与 `vt` 行数、确认 mtl 引用关系、用 MeshLab 打开看纹理与接缝。

### 第二部分：降分辨率省显存实测

4. 选 **coarse-neus** 阶段做 A/B（它的默认分辨率是固定 256×256，[configs/dreamcraft3d-coarse-neus.yaml:9-10](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-neus.yaml#L9-L10)，对比最干净）。先跑基线：

   ```sh
   # 终端 A：每秒记录显存
   nvidia-smi --query-gpu=memory.used --format=csv -l 1 > mem_default.csv
   # 终端 B：默认分辨率
   python launch.py --config configs/dreamcraft3d-coarse-neus.yaml --train \
     system.prompt_processor.prompt="$prompt" data.image_path="$image_path" \
     system.weights=<coarse-nerf的ckpt> trainer.max_steps=400 tag=mem-default
   ```

5. 再跑低分辨率组（README Tips 原样照抄）：

   ```sh
   nvidia-smi --query-gpu=memory.used --format=csv -l 1 > mem_lowres.csv
   python launch.py --config configs/dreamcraft3d-coarse-neus.yaml --train \
     system.prompt_processor.prompt="$prompt" data.image_path="$image_path" \
     system.weights=<coarse-nerf的ckpt> trainer.max_steps=400 tag=mem-lowres \
     data.height=128 data.width=128 data.random_camera.height=128 data.random_camera.width=128
   ```

6. 取两个 csv 的峰值（也可在训练日志或 TensorBoard 里交叉核对），写一段 200 字左右的分析，建议覆盖：显存峰值下降比例是否接近理论的激活值比例 \( (128/256)^2 = 1/4 \)？为什么下降不到 1/4（提示：扩散引导模型 DeepFloyd/Zero123 的权重与激活不随渲染分辨率变化、优化器状态、固定开销）？400 步内两组的 `save/` 渲染图质量差异如何？

**预期结果**：低分辨率组显存峰值显著下降但高于 1/4 基线；渲染图更糊但几何演化趋势一致。全部数值**待本地验证**——本讲义没有替你运行过这些命令，请以自己机器上的实测为准。

## 6. 本讲小结

- 导出器是与 geometry/renderer 平级的插件：`on_predict_start` 构建、`on_predict_epoch_end` 触发，与系统**共享**同一批训练好的组件实例，自身零权重。
- 纹理烘焙的核心技巧是"反向使用光栅化"：把 UV 网格投到 `texture_size` 的贴图平面上，`rast` 给出逐像素的面编号与重心坐标，插值 `v_pos` 得到"像素→表面点"反查表，再逐像素查询外观 MLP。
- xatlas 负责 UV 展开（分卡/参数化/打包），会拆分重编号顶点但保持面一一对应；`_unwrap_uv` 里构造的 `vmapping` 未被返回，烘焙靠的是"同面双编号"而非顶点映射。
- obj 落盘有两个格式细节：1-based 索引与 `vt` 的 \(1-v\) 翻转；贴图缝隙用 `cv2.inpaint` 修补，避免下游滤波采到黑边。
- 显存优化的落点：官方 Tips 是同时覆盖 `data` 与 `data.random_camera` 的分辨率；`16-mixed` 只用在 coarse 阶段；`fix_geometry` 缓存、`chunk_batch`、`cleanup()` 是代码内建的三处隐性优化；xformers/梯度检查点只存在于 DreamBooth 辅助脚本。

## 7. 下一步学习建议

本讲是单元八的第三讲，也是整个学习手册接近收尾处。建议：

1. **下一讲 u8-l4（Gradio 界面与二次开发实践）**会把导出与训练统一进一个 Web 界面（`launch.py --train --gradio` 分支训练结束后自动 `trainer.predict` 导出，[launch.py:197-199](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L197-L199)），并带你实现一个自定义扩展组件作为全手册的毕业实战。
2. 想继续深挖导出链路，可读 [threestudio/utils/saving.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py) 里 `_save_mtl` 未启用的 PBR 通道（`map_Pm/map_Pr/map_Bump`），思考要支撑它们需要把 material 换成哪个实现（仓库里有 `diffuse-with-point-light-material` 可对照）。
3. 想验证自己对烘焙坐标链路的理解，可以做一个"定向实验"：临时把 `uv_padding` 换成把洞填成纯红（**示例代码**，验完还原），导出后在 MeshLab 里放大图卡边缘，直接看到隔离带的位置与宽度，再对照 `xatlas_pack_options.padding` 的取值。
