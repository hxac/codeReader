# 器件网格 DeviceGrid 的生成

## 1. 本讲目标

上一篇（u2-l2）我们看清了架构 XML 解析后得到的「物理瓦片类型 `t_physical_tile_type`」与「逻辑块类型 `t_logical_block_type`」两组抽象。本讲要回答一个紧接着的问题：**这些瓦片在芯片上究竟摆成一张什么样的网格？网格有多大？**

读完本讲，你应当能够：

- 看懂 `DeviceGrid` 如何用一个三维矩阵表示整块 FPGA 的物理布局，并能说出每个格子里存的四个字段。
- 理解 `create_device_grid` 的两个重载、`auto` 与命名（`fixed`）布局的区别，以及 `fixed_by_rr_graph` 这个「禁止改尺寸」开关的含义。
- 掌握自动定尺寸（auto-sizing）循环的工作原理：宽高比、资源需求、目标利用率三者如何共同决定最终的网格宽高。
- 跟踪一条完整调用链：从 `vpr_create_device_grid` 出发，到 `create_device_grid`，再到 `build_device_grid`，最终写入 `device_ctx.grid`。

本讲是 u2（架构描述与解析）的收尾，也是后续布局（第 5 单元）、布线（第 6 单元）的地基——没有网格，布局器就不知道把逻辑块放到哪里，布线器就不知道节点在哪里。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 为什么要有一张「网格」

真实 FPGA 的芯片面积被划分成一个个**瓦片（tile）**，例如 IO 瓦片排在四周、逻辑瓦片（LAB/CLB）排成阵列、RAM/DSP 等特殊瓦片散布其中。VPR 把这块芯片抽象成一张二维（单层）或三维（多层堆叠/2.5D）网格：

- 每个网格位置 \((x, y)\)（或 \((layer, x, y)\)）存放一个 `t_grid_tile`。
- 这个 `t_grid_tile` 记录「这里摆的是什么类型的瓦片」，以及「我是不是某个大瓦片的左下角根节点」。

布局阶段就是把聚簇后的逻辑块绑定到这些网格位置上；布线阶段就是在网格之间铺设可布线资源。**所以网格是器件的物理坐标系，是所有空间概念的根。**

### 2.2 auto 布局 vs fixed 布局

架构 XML 的 `<layout>` 区段里，用 `<auto_layout>` 或 `<fixed_layout>` 来描述「瓦片该怎么摆」。这两者对应枚举 `e_grid_def_type::AUTO` / `FIXED`：

- **AUTO（自动布局定义）**：只给一套「瓦片摆放规则」（例如「四周摆 IO、中间摆 LAB」）和宽高比 `aspect_ratio`，不写死尺寸。网格大小在运行时根据电路需要多少资源动态撑大。
- **FIXED（固定布局定义）**：写死了 `width` / `height` 和一个名字 `name`，是一块「定尺寸的成品芯片」。

关键点：**架构里描述的是「摆放规则 + 尺寸约束」，而不是一张已经填好的网格。** 把规则在某个目标宽高下「实例化」成真正的 `t_grid_tile` 矩阵，正是 `build_device_grid` 的工作。

### 2.3 宽高比与目标利用率

- **宽高比（aspect ratio）**：网格高与宽的比例。若 `aspect_ratio = 1.0` 则正方形，`0.75` 则偏宽。给定宽度 \(W\) 与宽高比 \(r\)，高度 \(H\) 由下式求得：

\[
H = \mathrm{round}(W / r)
\]

- **目标利用率（target device utilization）**：你希望电路占满芯片面积的最大比例。例如 `0.6` 表示「挑一块电路面积只占六成的芯片，留四成给布线余量」。利用率越低，器件越大、布线越轻松、面积越浪费；利用率越高，器件越小、布线越紧张。

### 2.4 NdMatrix：本讲会用到的容器

`DeviceGrid` 内部用 `vtr::NdMatrix<t_grid_tile, 3>` 存储网格，这是一个三维稠密矩阵，可以用 `grid[layer][x][y]` 这种自然下标访问。它是 `libvtrutil` 提供的通用多维矩阵（u9-l1 会详讲），这里只要知道「它是一个三维数组」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [device_grid.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/device_grid.h) | `DeviceGrid` 类与 `t_grid_tile` 结构的定义，是网格的「数据表示」。 |
| [setup_grid.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.h) | `create_device_grid` 等函数的声明，是「网格生成」的对外接口。 |
| [setup_grid.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp) | `create_device_grid` / `auto_size_device_grid` / `build_device_grid` 等的全部实现，本讲的重头戏。 |
| [vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | `vpr_create_device_grid`，在主流程中统计资源需求并调用 `create_device_grid`。 |
| [grid_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/grid_types.h) | `t_grid_loc_spec` / `t_grid_loc_def` / `e_grid_def_type`，描述「摆放规则」的原始结构。 |
| [physical_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h) | `t_grid_def`（一条布局定义）与 `t_physical_tile_loc`（一个三维坐标）。 |

## 4. 核心概念与源码讲解

### 4.1 DeviceGrid 数据结构：网格长什么样

#### 4.1.1 概念说明

`DeviceGrid` 是 FPGA 晶圆面积的完整抽象。它对外暴露「网格有几层、多宽、多高」「某个坐标是什么瓦片」「某种瓦片共有几个实例」等查询，把「一整块芯片」装进一个对象。这个对象最终被存进全局 `DeviceContext.grid`，被几乎所有后续阶段共享。

它有三件关键内部家当：

1. **三维矩阵 `grid_`**：`vtr::NdMatrix<t_grid_tile, 3>`，下标顺序是 `[layer][x][y]`，即第一维是层号（0 为底层），第二、三维是 x、y 坐标。注意**这是 3D**，支持 2.5D/3D 多 die 堆叠架构；传统单层 FPGA 就是 `num_layers == 1` 的特例。
2. **每个格子 `t_grid_tile`**：最小单位，记录瓦片类型与偏移。
3. **实例计数 `instance_counts_`**：每种瓦片类型在每层有多少个实例，构造时由 `count_instances()` 一次性算好缓存起来，供定尺寸阶段反复查询。

#### 4.1.2 核心流程

`DeviceGrid` 的生命周期非常简单：

```
构造期：build_device_grid 把实例化好的 NdMatrix<t_grid_tile,3> 连同 grid_def 喂给构造函数
        → 构造函数调用 count_instances() 缓存「每种瓦片每层多少个」
使用期：外部只读查询（width/height/get_physical_type/num_instances/...）
销毁期：clear()
```

之所以要在构造时就把实例数算好，是因为定尺寸循环（4.3 节）会反复「建一个候选网格 → 问它够不够资源」，如果每次现数要遍历整张网，循环会很慢。

#### 4.1.3 源码精读

先看最小单位 `t_grid_tile`，四个字段一目了然：

> [device_grid.h:15-20](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/device_grid.h#L15-L20) —— 一个网格瓦片 = 类型指针 + 两个偏移 + 元数据。`type` 指向上一讲讲过的 `t_physical_tile_type`，`nullptr` 表示非法位置；`width_offset/height_offset` 用来标记「我是不是某个大瓦片（width/height>1）内部的格子」，根节点（左下角）的偏移为 0。

一个 width=2、height=3 的大瓦片会占用 6 个相邻格子，其中只有左下角那格 `width_offset==0 && height_offset==0`，称为**根位置（root location）**；其余 5 格的偏移非零，指明自己相对根的偏移量。这种「一个实例占多格」的设计让大块 RAM/DSP 能与普通 LAB 统一在一张网格里表达。

再看 `DeviceGrid` 类的尺寸查询接口：

> [device_grid.h:79-89](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/device_grid.h#L79-L89) —— `get_num_layers()` 取第一维大小，`width()`/`height()` 取第二、三维大小，三者正好对应 `[layer][x][y]` 的三维布局。`dim_sizes()` 一次性返回三元组。

接着是几个高频查询方法。注意 `t_physical_tile_loc` 是一个三维坐标 `{x, y, layer_num}`：

> [physical_types.h:770-785](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L770-L785) —— `t_physical_tile_loc` 把「层 + x + y」打包成一个坐标对象，是访问网格的通用钥匙。

> [device_grid.h:118-145](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/device_grid.h#L118-L145) —— 一组坐标 → 瓦片信息的查询：`get_physical_type()` 取类型、`get_width_offset()`/`get_height_offset()` 取偏移、`is_root_location()` 判定是否根位置、`get_tile_bb()` 返回该瓦片的包围盒矩形。

`get_tile_bb()` 的逻辑值得记住：它先用当前坐标减去偏移得到根坐标，再用 `type->width/height` 算出包围盒，即：

\[
x_{\text{low}} = x - \text{width\_offset},\quad x_{\text{high}} = x_{\text{low}} + \text{width} - 1
\]

这就把「任意一个被大瓦片覆盖的格子」还原成「整个瓦片占用的矩形」。

最后看两个「元信息」接口，它们在本讲后面会反复用到：

> [device_grid.h:104-115](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/device_grid.h#L104-L115) —— `num_instances(type, layer_num)` 查某层（`-1` 表示全层合计）某瓦片有多少实例；`limiting_resources()` 返回「制约器件尺寸的逻辑块类型」列表；`fixed_by_rr_graph()`/`set_fixed_by_rr_graph()` 是「尺寸是否被外部 RR 图钉死」的开关。

`fixed_by_rr_graph` 这个开关很关键：当用户通过 `--read_rr_graph` 读入了一个预先建好的布线资源图时，网格尺寸必须与那张图完全一致，**不允许任何方向的拉伸或收缩**。此时 `create_device_grid` 会直接原样返回已有网格（见 4.2.3）。

#### 4.1.4 代码实践

- **实践目标**：亲手验证「一个大瓦片如何在网格里铺成多格、根位置在哪」。
- **操作步骤**：
  1. 在 `device_grid.h` 中找到 `t_grid_tile`、`get_physical_type`、`get_width_offset`、`get_height_offset`、`is_root_location`、`get_tile_bb` 六个符号。
  2. 假设某瓦片 `type->width = 2, type->height = 3`，其根放在 `(x=4, y=5)`。手算：它覆盖哪 6 个格子？哪些格子的 `width_offset/height_offset` 分别是多少？
- **需要观察的现象**：根位置 `(4,5)` 的两个 offset 都是 0；`(4,6)` 的 offset 是 `(0,1)`；`(5,7)` 的 offset 是 `(1,2)`；最右上角 `(5,7)` 也不是根。
- **预期结果**：包围盒 `get_tile_bb()` 返回 `{{4,5},{5,7}}`。任意非根格子调用 `get_root_location()`（[device_grid.h:148-154](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/device_grid.h#L148-L154)）都应回到 `(4,5)`。

#### 4.1.5 小练习与答案

**练习 1**：`DeviceGrid` 内部为什么用 `NdMatrix<t_grid_tile, 3>` 而不是 `vector<vector<t_grid_tile>>`？

**参考答案**：因为现代 VTR 支持 2.5D/3D 多 die 架构，需要 layer 这第三维；并且 `NdMatrix` 提供统一的 `[layer][x][y]` 下标与连续内存，比手写嵌套 vector 更安全（带边界断言）、更高效。

**练习 2**：`is_root_location()` 为什么用 `get_width_offset==0 && get_height_offset==0` 判断，而不是看 `type` 指针？

**参考答案**：同一个大瓦片的所有格子 `type` 指针都相同，无法区分根与非根；而偏移量在根处为 0、其他处非 0，正好唯一标识根位置。

---

### 4.2 create_device_grid 决策逻辑：选哪套规则、定多大

#### 4.2.1 概念说明

有了数据表示，下一个问题是「谁来决定网格多大」。这就是 `setup_grid` 模块的职责。它对外暴露一个工厂函数 `create_device_grid`，输入是「架构里的布局规则列表 + 电路的资源需求」，输出是一个填好的 `DeviceGrid`。

核心决策有三类：

1. **layout_name == "auto"**：自动定尺寸，由电路需求 + 宽高比 + 目标利用率共同决定大小。
2. **layout_name == 某个 fixed 布局的名字**：用那块定尺寸芯片，大小写死。
3. **fixed_by_rr_graph 为真**：直接返回已有网格，不动尺寸。

`create_device_grid` 有**两个重载**，分别面向「按资源定尺寸」和「按显式宽高定尺寸」两种调用场景，它们共享同一套 `auto`/`named` 内部分支。

#### 4.2.2 核心流程

先看资源驱动重载（最常用）的决策树：

```
create_device_grid(layout_name, grid_layouts, min_counts, utilization, fixed_width)
  ├─ grid.fixed_by_rr_graph()?  → 直接返回现有 grid（禁止改尺寸）
  ├─ layout_name == "auto"?
  │     ├─ fixed_width > 0?     → compute_auto_layout_height(width) → 转入显式宽高重载
  │     └─ 否                   → auto_size_device_grid(...)（见 4.3）
  └─ layout_name == 命名布局?
        → find_if 匹配名字 → build_device_grid(def, def.width, def.height)
```

显式宽高重载的决策树类似，区别在于 `auto` 分支会用 `build_device_grid` 直接按给定宽高实例化（若架构是 fixed 系列，则在 fixed 列表里挑最接近的）。

#### 4.2.3 源码精读

先看两个辅助函数，它们把「宽高比」这件事封装好了：

> [setup_grid.cpp:72-94](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L72-L94) —— `get_auto_layout_aspect_ratio()` 在布局列表里找唯一一条 `AUTO` 规则并返回它的 `aspect_ratio`（找不到返回 `nullopt`）；`compute_auto_layout_height()` 用 `vtr::nint(width / aspect_ratio)` 把宽度换算成高度；`has_fixed_device_size()` 判定器件尺寸是否被钉死（命名布局，或 `auto + device_width>0`）。

注意 `compute_auto_layout_height` 用的正是 2.3 节那道公式 \(H = \mathrm{round}(W/r)\)。

再看「按资源定尺寸」这个主重载：

> [setup_grid.cpp:97-106](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L97-L106) —— 函数入口先检查 `fixed_by_rr_graph()`：若为真，断言现有网格尺寸大于 0 后**原样返回**，跳过一切定尺寸逻辑。这是「外部 RR 图钉死尺寸」的总闸门。

> [setup_grid.cpp:108-116](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L108-L116) —— `auto` 分支的二级判断：用户若给了 `--device_width`（`fixed_device_width > 0`），就由宽度按宽高比算高度，转调另一个重载；否则进入 `auto_size_device_grid` 做资源驱动定尺寸。

> [setup_grid.cpp:117-139](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L117-L139) —— 命名布局分支：用 `find_if` 按名字在 `grid_layouts` 里找匹配的 `t_grid_def`；找不到就把所有合法名字拼成错误串，用 `VPR_FATAL_ERROR(VPR_ERROR_ARCH, ...)` 抛致命错。找到则用其写死的 `width/height` 调 `build_device_grid`。

这里体现了一条重要的架构理念呼应 u2-l1/u2-l2：**「架构驱动」**。函数本身不写死任何「芯片长这样」的假设，所有尺寸与摆放都来自运行时传入的 `grid_layouts`（来自架构 XML）和 `num_type_instances`（来自电路网表）。算法代码只做决策。

「按显式宽高定尺寸」重载（用于分析式布局的合法化阶段等场景）：

> [setup_grid.cpp:143-207](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L143-L207) —— 与主重载结构对称：同样先过 `fixed_by_rr_graph` 闸门；`auto` 分支里，若架构首条是 `AUTO` 规则就直接按给定宽高 `build_device_grid`，否则把所有 fixed 布局按尺寸排序，挑第一个「不小于目标宽高」的，找不到就用最大那块并告警。

最后看「网格统计」函数，它会输出一行被回归测试解析的日志：

> [setup_grid.cpp:810-814](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L810-L814) —— `report_device_grid_stats` 打印 `FPGA sized to W x H: N grid tiles (name)`。注释提醒：这行格式被 VTR 的任务解析器当作 QoR 指标抓取，**改格式要同步更新解析器正则**。

#### 4.2.4 代码实践

- **实践目标**：跟踪 `vpr_create_device_grid` 如何调用 `create_device_grid`，画出「谁喂了什么参数」。
- **操作步骤**：
  1. 打开 [vpr_api.cpp:595-623](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L595-L623)，找到 `vpr_create_device_grid` 的实现。
  2. 注意它如何遍历 `cluster_ctx.clb_nlist.blocks()` 统计每种逻辑块类型需要多少实例，得到 `num_type_instances`。
  3. 注意它把 `vpr_setup.device_layout`（默认 `"auto"`）、`vpr_setup.PackerOpts.target_device_utilization`（默认 `1.0`）、`vpr_setup.device_width`（默认 `0`）原样传给 `create_device_grid`。
- **需要观察的现象**：传给 `create_device_grid` 的是**逻辑块类型**→数量的映射，而 `DeviceGrid` 内部存的是**物理瓦片类型**；二者通过上一讲讲的 `equivalent_tiles` 打通。
- **预期结果**：能写出调用链：`vpr_create_device` → `vpr_create_device_grid` → `create_device_grid(资源驱动重载)` → `auto_size_device_grid` 或 `build_device_grid`。**待本地验证**：默认参数下（`--device auto`、无 `--device_width`），走的是 `auto_size_device_grid` 分支。

#### 4.2.5 小练习与答案

**练习 1**：用户既没指定 `--device` 也没指定 `--device_width`，`create_device_grid` 会走哪个分支？

**参考答案**：`--device` 默认 `"auto"`、`--device_width` 默认 `0`（见 [read_options.cpp:1812-1822](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1812-L1822)），所以进入 `auto` 分支且 `fixed_device_width==0`，最终调用 `auto_size_device_grid`。

**练习 2**：为什么 `create_device_grid` 开头要先检查 `fixed_by_rr_graph()`？

**参考答案**：当外部用 `--read_rr_graph` 提供了现成的布线资源图时，网格尺寸必须与该图严格一致，否则节点坐标对不上。所以一旦网格被标记为「由 RR 图钉死」，就必须原样返回、禁止拉伸或收缩。

---

### 4.3 器件尺寸估算：auto 循环怎么把网格撑到刚好够用

#### 4.3.1 概念说明

`auto_size_device_grid` 是本讲的算法核心。它解决的问题是：**给定一套 AUTO 摆放规则、电路的资源需求、以及目标利用率，找出「最小的、能装下电路且利用率不超过上限」的网格尺寸。**

它的策略朴素而稳健：**从最小尺寸（3×3）开始，逐步把宽度加 1，每次重建候选网格并检查是否满足条件，满足就返回。** 宽度每加 1，高度按宽高比同步缩放，所以网格是「等比例放大」的。

这里有两个判定条件，缺一不可：

1. **资源够**：每种逻辑块类型需要的实例数，都能被网格里对应的等价物理瓦片容纳（且考虑「最不灵活的类型优先分配」）。
2. **利用率不超标**：电路占用面积占网格面积的比例，不超过 `target_device_utilization`。

第 2 个条件用 `calculate_device_utilization` 计算，它本质是个面积比。

#### 4.3.2 核心流程

`auto_size_device_grid` 主循环（AUTO 分支）伪代码：

```
找到唯一的 AUTO 规则 grid_def
max_size = 所有 minimum_instance_counts 之和 × MAX_SIZE_FACTOR(10000)   # 防死循环上限
width, height = 3, 3
loop:
    height = round(width / aspect_ratio)          # 等比例缩放
    grid = build_device_grid(grid_def, width, height, warn=false)
    if grid_satisfies_instance_counts(grid, min_counts, max_util):
        return grid                                # 命中：刚好够用且利用率达标
    width += 1
    if width*height > max_size:
        fatal("装不下，资源不随网格增长（如固定数量的 PLL）")
```

`grid_satisfies_instance_counts` 又分两步：

```
1) overused = grid_overused_resources(grid, counts)   # 资源够不够
2) utilization = calculate_device_utilization(grid, counts)  # 利用率
return overused.empty() && utilization <= max_util
```

`grid_overused_resources` 的分配策略很巧妙：**按「等价瓦片数从少到多」排序逻辑块类型，先分配最不灵活的（等价瓦片最少的），再分配灵活的。** 这是因为不灵活的类型最容易「无瓦可放」，先满足它们才能正确判断整体可行性。

`calculate_device_utilization` 的面积比定义：

\[
\text{utilization} = \frac{\text{instance\_area}}{\text{grid\_area}}
\]

其中 `grid_area` 是网格里每个根瓦片的 `width×height` 之和，`instance_area` 是电路所需每种逻辑块（折算到其代表物理瓦片，并按 `capacity` 平摊）的 `width×height` 之和。

#### 4.3.3 源码精读

先看循环的上界与起点：

> [setup_grid.cpp:230-247](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L230-L247) —— `max_size = total_minimum_instance_counts * MAX_SIZE_FACTOR`（`MAX_SIZE_FACTOR` 为 10000，见 [setup_grid.cpp:29](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L29)）；初始 `width = height = 3`，注释说明这是「避免 `<perimeter>` 起止位置问题」的最小尺寸。

再看主循环体：

> [setup_grid.cpp:248-276](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L248-L276) —— 每轮：`height = nint(width/aspect_ratio)` 等比例缩放；`build_device_grid(..., warn_out_of_range=false)` 重建候选网格（小尺寸时关闭越界警告，因为摆放规则的起止在小网格下可能暂时越界、属正常）；`grid_satisfies_instance_counts` 命中即返回；否则 `width++` 继续。`limiting_resources` 会被 `grid_overused_resources` 回填，记录「哪些资源正在制约尺寸」。

> [setup_grid.cpp:279-282](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L279-L282) —— 超过 `max_size` 仍装不下则抛致命错，并提示「可能是某些资源（如 Titan Stratix IV 里的 PLL）数量不随网格增长，撑多大都不够」——这是一个很有诊断价值的提示。

接着看两个判定函数：

> [setup_grid.cpp:378-394](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L378-L394) —— `grid_satisfies_instance_counts`：先看有没有 overused 资源，再看利用率是否超过上限，两个条件都满足才返回 `true`。注意 `target_device_utilization` 在这里被当作**上限**（maximum）使用——这印证了 [setup_grid.cpp:114-116](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L114-L116) 注释「treat the target device utilization as a maximum」。

> [setup_grid.cpp:340-373](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L340-L373) —— `grid_overused_resources` 的「最不灵活优先」分配：用 `stable_sort` 按 `equivalent_tiles.size()` 升序排逻辑块类型，逐类从可用瓦片池里扣除需求，扣不光的记为 overused。

利用率计算在 `stats.cpp`：

> [stats.cpp:800-848](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/stats.cpp#L800-L848) —— `calculate_device_utilization`：`grid_area` 由网格所有**根位置**瓦片的 `width×height` 累加；`instance_area` 由电路每种逻辑块的代表物理瓦片面积累加，且对 `capacity>1` 的多容量块按 `type_area /= capacity` 平摊；最后返回比值。只数根位置是为了避免大瓦片的多格被重复计入 `grid_area`。

最后看「摆放规则实例化」的核心 `build_device_grid`：

> [setup_grid.cpp:396-434](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L396-L434) —— 对 FIXED 规则强校验「请求尺寸 == 定义尺寸」；按 `num_layers × width × height` 三维 resize 网格与优先级矩阵；把全网格初始化为 `EMPTY_PHYSICAL_TILE_TYPE`（优先级设为最低，确保后续任何规则都能覆盖它）。

> [setup_grid.cpp:454-591](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L454-L591) —— 对每条 `t_grid_loc_def` 摆放规则：把表达式里的变量 `W/H/w/h`（器件宽高、瓦片宽高）注入；用 `FormulaParser` 解析 x/y 的 `start/end/incr/repeat` 四个表达式；做大量合法性校验（start≤end、incr≥块尺寸、repeat≥区域尺寸，避免重叠）；最后双重循环按 `repeat`（区域重复）和 `incr`（区域内块间距）把瓦片逐个 `set_grid_block_type` 落到网格上。

这里的四个表达式正是来自架构 XML 的「摆放规则」——它们之所以是字符串表达式而非整数，是为了能写 `W-1`、`max(w+1,W)` 这种「随器件尺寸自适应」的位置。这正是 AUTO 布局能在任意宽高下实例化的关键。规则的默认值见 [grid_types.h:96-102](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/grid_types.h#L96-L102)：x 默认 `start=0, end=W-1, incr=w`（按块宽填满一行），y 同理——即「把整张网格铺满这种瓦片」。

冲突仲裁由优先级决定：

> [setup_grid.cpp:612-660](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L612-L660) —— `set_grid_block_type`：收集目标区域所有受影响格子的当前最高优先级；新规则优先级**严格更高**才覆盖，**相等**则「后到的赢」并告警，**更低**则不覆盖。这解释了为什么 XML 里 `<perimeter>`（IO，高优先级）能压过 `<fill>`（LAB，低优先级）：边界格子优先给 IO。

> [setup_grid.cpp:605-609](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L605-L609) —— 网格建好后包成 `DeviceGrid` 并立即调 `check_grid` 做一致性自检（每个根瓦片的覆盖区类型与偏移必须自洽），自检失败直接致命错。

把整条链拉通看上层入口：

> [vpr_api.cpp:563-588](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L563-L588) —— `vpr_create_device` 在主流程里依次创建网格、设时钟网络、设 NoC、再建 RR 图。

> [vpr_api.cpp:595-623](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L595-L623) —— `vpr_create_device_grid`：遍历聚簇网表统计 `num_type_instances`（资源需求），调 `create_device_grid`（资源驱动重载），把结果写入 `device_ctx.grid`，最后 `report_device_grid_stats` 输出尺寸。

#### 4.3.4 代码实践

- **实践目标**：亲手把「auto 定尺寸循环」在纸面跑一遍，理解宽高比与利用率如何共同决定最终尺寸。
- **操作步骤**：
  1. 假设某架构 `aspect_ratio = 1.0`（正方形），`target_device_utilization = 1.0`，电路只需要 4 个 LAB，每个 LAB 是 1×1 瓦片，且 LAB 占满网格内部、四周各 1 圈 IO（不计入利用率分母的实例）。
  2. 从 `width=3` 开始手算：`height=round(3/1.0)=3`，3×3 网格扣掉边界 IO 后内部只有 1×1=1 个 LAB 位 → 不够 4 个 → `width++`。
  3. 继续算 `width=5`（5×5，内部 3×3=9 个 LAB 位）、`width=4`（4×4，内部 2×2=4 个 LAB 位）。
  4. 判断：`width=4` 时内部正好 4 个 LAB 位，够装；利用率 = 4/4 = 1.0 ≤ 上限 1.0 → 命中。
- **需要观察的现象**：循环并不是「直接算出最优尺寸」，而是从 3×3 起逐个宽度尝试，命中即停。`aspect_ratio` 决定高度跟着宽度等比例走；`target_device_utilization` 决定「要多松」。
- **预期结果**：最终网格 4×4。若把 `target_device_utilization` 改成 0.5，则 4×4 利用率 1.0 超标，循环会继续放大直到内部 LAB 位 ≥ 8（利用率 ≤ 0.5）。**待本地验证**：用 `vpr ... --target_utilization 0.5` 对比默认 1.0，观察日志 `FPGA sized to W x H` 的差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 auto 循环从 `3×3` 开始而不是 `1×1`？

**参考答案**：因为 `<perimeter>` 类摆放规则（IO 摆四周）在 1×1、2×2 时会出现「起止位置重叠/越界」的问题，3×3 是能正确铺出边界与内部的最小尺寸（见 [setup_grid.cpp:241-245](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L241-L245) 注释）。

**练习 2**：`MAX_SIZE_FACTOR = 10000` 的作用是什么？如果去掉会怎样？

**参考答案**：它是「循环上界保险」。若某资源（如固定数量的 PLL）不随网格增长，循环可能永远凑不齐而无尽放大。乘 10000 给了一个「已远超需求仍装不下」的判定阈值，到上限就报致命错并给出诊断提示（[setup_grid.cpp:279-282](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L279-L282)），避免死循环。

**练习 3**：`calculate_device_utilization` 在累加 `grid_area` 时为什么只数「根位置」瓦片？

**参考答案**：大瓦片（如 2×3 的 RAM）占多格，若每格都计入会把面积重复算 6 次。只数根位置（offset 全 0）确保每瓦片计一次 `width×height`，得到正确的网格总面积。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从架构 XML 到网格生成」的完整追踪。

**任务**：选一个真实架构（例如 `vtr_flow/arch/` 下的 k6_frac_N10_mem32K_40nm.xml 之类，或本仓库 `doc/src/quickstart` 提到的示例），完成以下步骤并记录结论。

1. **读架构**：打开架构 XML 的 `<layout>` 区段，找出它是 `<auto_layout>` 还是若干 `<fixed_layout>`。若是 auto，记下 `aspect_ratio`；若是 fixed，记下各布局的 `name/width/height`。对照 [read_xml_arch_file.cpp:2593-2622](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L2593-L2622) 确认 `<auto_layout>` 会被命名为 `"auto"`、`aspect_ratio` 默认 1.0。
2. **画调用链**：从 `vpr_create_device_grid`（[vpr_api.cpp:595](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L595)）出发，画出在你这套命令行参数（默认 `--device auto`）下会经过 `create_device_grid` 哪个分支、最终落到 `auto_size_device_grid` 还是 `build_device_grid`。
3. **实跑对比**：用同一个电路分别跑 `--target_utilization 1.0` 和 `--target_utilization 0.5`，从日志里抓 `FPGA sized to W x H: N grid tiles (name)` 行（由 [setup_grid.cpp:810-814](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L810-L814) 输出），验证利用率更低时网格更大。
4. **进阶（可选）**：在 `auto_size_device_grid` 主循环里临时加一行 `VTR_LOG("try %zux%zu\n", width, height);`（或用已有的 `#define VERBOSE`，见 [setup_grid.cpp:252-254](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_grid.cpp#L252-L254)），重新编译运行，观察逐次放大的尝试序列。注意：这属于本地调试改动，按项目规范**不要提交**。

**预期产出**：一张调用链图 + 三组 `FPGA sized to` 日志对比 + 一句对「宽高比与利用率如何影响最终尺寸」的总结。

## 6. 本讲小结

- `DeviceGrid` 用三维 `NdMatrix<t_grid_tile, 3>`（`[layer][x][y]`）表示整块 FPGA，每格存「类型指针 + 两个偏移 + 元数据」；大瓦片占多格，靠偏移量区分根位置。
- 网格生成对外只有 `create_device_grid` 一个工厂函数，两个重载分别面向「按资源定尺寸」和「按显式宽高定尺寸」，内部按 `auto`/命名布局分支。
- `auto` 定尺寸是一个从 3×3 起、宽度逐次 +1、高度按宽高比等比例缩放的循环，命中「资源够 + 利用率不超标」即停。
- 利用率是面积比 `instance_area/grid_area`，`target_device_utilization` 当作上限使用；`calculate_device_utilization` 只数根位置瓦片避免重复计数。
- 优先级仲裁（`set_grid_block_type`）让 `<perimeter>` 等高优先级规则覆盖 `<fill>` 等低优先级规则，解决瓦片摆放冲突。
- `fixed_by_rr_graph` 开关在外部提供 RR 图时锁死尺寸；整条链体现了「架构驱动」——算法不写死任何尺寸假设，全来自运行时的 XML 与网表。

## 7. 下一步学习建议

本讲讲完了「器件物理网格如何生成」，至此 u2（架构描述与解析）单元结束。网格是空间坐标系，但要真正做布局布线，还需要：

- **网表数据结构**：进入 u3，先看 [u3-l1 Netlist 泛型基类](u3-l1-netlist-base.md)，理解电路如何被抽象成「块/端口/引脚/线网」四元模型；这是要被摆进网格的东西。
- **全局状态**：接着看 [u3-l4 VprContext 与全局状态管理](u3-l4-vpr-context.md)，理解本讲写入的 `device_ctx.grid` 如何被各阶段共享读取。
- **主流程编排**：看 [u3-l5 主流程编排 vpr_api](u3-l5-vpr-api-orchestration.md)，把本讲的 `vpr_create_device` 放回整条 `vpr_flow` 的时序中。

如果你对「网格上的可布线资源如何铺出来」更感兴趣，可以提前跳到 u6 的 [u6-l1 路由资源图 RR Graph](u6-l1-rr-graph.md)——RR 图正是建在本讲生成的网格之上的。
