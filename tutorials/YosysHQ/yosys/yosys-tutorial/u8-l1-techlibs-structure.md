# techlibs 工艺映射库的结构

## 1. 本讲目标

本讲是「专家层·工艺库与目标平台」的第一讲。学完之后,你应该能够:

- 说出 `techlibs/` 目录为什么是一堆 `.v` 文件而不是 `.cc` 文件,以及它们在运行时如何被加载。
- 区分三种容易混淆的库文件:`*_sim.v`(仿真模型)、`*_map.v`(映射模板)以及 common 目录里那个没有 `_sim/_map` 后缀的 `techmap.v`。
- 理解 common 公共库三件套 `simlib.v` / `simcells.v` / `techmap.v` 各自扮演的角色。
- 解释 `techmap` pass 如何用 `+/` 路径定位这些库,以及 `-map`、`-lib`、`-specify` 三种加载方式的差别。
- 把 techlibs 目录的物理结构与「前端 → pass → 后端」数据流对应起来。

本讲建立在 [u6-l5 techmap 与 simplemap](u6-l5-techmap-simplemap.md) 之上——你已经知道 techmap 是一台「模板替换机器」,本讲就回答「那些模板从哪里来、由谁组织」。

## 2. 前置知识

在进入源码之前,先用三段话建立直觉。

**为什么 techlibs 是「数据」而不是「代码」?**
回顾 [u6-l5](u6-l5-techmap-simplemap.md):techmap 的工作方式是「读入一个映射库,按 cell 类型匹配模板模块,把模板内容内联进当前模块」。这意味着映射规则本身是用**普通 Verilog 模块**写的。既然是 Verilog,它就应该是 `.v` 文件,而不是写死在 C++ 里。techlibs 目录就是存放这些 `.v`「数据文件」的仓库——它是算法的「喂料」,本身不实现任何算法。这一点在 [u1-l3 顶层目录结构](u1-l3-directory-structure.md) 中已经埋下伏笔:techlibs 属于「资源层」。

**一条综合流水线需要哪几种「库」?**
把一个设计从高层 `$` 单元综合到目标芯片的真实原语,通常要喂给 yosys 三类信息:

| 角色 | 作用 | 典型文件 | 加载方式 |
|---|---|---|---|
| 仿真模型(simulation) | 告诉 yosys 某个单元「功能上做什么」、定时如何 | `*_sim.v` | `read_verilog -lib [-specify]` |
| 映射模板(mapping) | 告诉 techmap「如何把一个通用 cell 替换成若干目标单元」 | `*_map.v`、`techmap.v` | `techmap -map` |
| 黑盒库(blackbox) | 只声明端口,让 yosys「认识」某个原语名而不关心其内部 | 任意 `*_sim.v` 加 `-lib` | `read_verilog -lib` |

注意:**同一个 `.v` 文件可以同时充当「仿真模型」和「黑盒库」**,区别只在加载时是否带 `-lib`。这是理解 techlibs 的关键反直觉点。

**`_sim` 与 `_map` 后缀到底什么意思?**
这是 yosys 社区约定俗成的命名规范,不是语法强制:

- `_sim` = simulation,「这个单元功能上是什么样、仿真时怎么表现」。它描述**行为**,通常带 `assign` 或 `always`,组合单元还常带 `specify` 定时块。
- `_map` = mapping,「怎么把一个通用 cell 映射(替换)成它」。它描述**改写规则**,核心标志是模板里出现 `_TECHMAP_REPLACE_`、`techmap_celltype` 等 techmap 专用属性(详见 [u6-l5](u6-l5-techmap-simplemap.md))。

一个完整的厂商流程往往成对出现:`cells_sim.v`(描述目标芯片原语的功能)+ `cells_map.v`(描述如何把通用 cell 变成这些原语)。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `techlibs/CMakeLists.txt` | techlibs 顶层入口,逐个 `add_subdirectory` 引入各厂商目录 |
| `techlibs/common/CMakeLists.txt` | 把 common 的 `.v` 库声明为 `synth` pass 的 `DATA_FILES`,决定安装到共享目录 |
| `techlibs/common/simlib.v` | 高层 `$` 单元(`$not`、`$and`...)的仿真模型库(104 个模块) |
| `techlibs/common/simcells.v` | 门级 `$_` 单元(`$_NOT_`、`$_AND_`...)的仿真模型库(149 个模块),abc 的输入约定 |
| `techlibs/common/techmap.v` | 把高层 `$` 单元映射到门级 `$_` 单元的通用映射库(techmap 默认库) |
| `passes/techmap/techmap.cc` | techmap pass 本体,含「无 `-map` 时默认加载 `+/techmap.v`」的逻辑 |
| `techlibs/xilinx/cells_sim.v` | Xilinx 真实原语(LUT6、FDRE、CARRY4...)的仿真模型(99 个模块) |
| `techlibs/xilinx/cells_map.v` | 把通用 cell 映射成 Xilinx 原语的 techmap 模板(9 个模块) |
| `techlibs/xilinx/lut_map.v` | 把 `$lut` 映射成 LUT1..LUT6 / MUXF5 的 techmap 模板 |
| `techlibs/xilinx/CMakeLists.txt` | 声明 `synth_xilinx` pass 及其 `DATA_FILES`(cells_map/sim/xtra 等) |
| `techlibs/xilinx/synth_xilinx.cc` | Xilinx 专属 ScriptPass,演示如何在脚本里用 `+/` 引用这些库 |

## 4. 核心概念与源码讲解

### 4.1 techlibs 的组织与运行时加载机制

#### 4.1.1 概念说明

`techlibs/` 顶层是一个**按厂商/平台分目录**的资源仓库。每个子目录对应一个目标平台(Xilinx、iCE40、Intel、Gowin...),里面装的是该平台需要的 `.v` 库与 `.txt` 资源(如 BRAM 描述)。`common/` 是一个特殊子目录——它不属于任何厂商,而是提供 yosys **内置通用**综合所需的公共库。

这些 `.v` 文件本身不参与 C++ 编译,而是被 CMake 以 `DATA_FILES` 的形式**安装到 yosys 的共享数据目录**(俗称 share dir)。运行时,脚本里写的 `+/techmap.v`、`+/xilinx/cells_sim.v` 这种 `+/` 前缀路径,就是指「share dir 根目录下的相对路径」。换句话说,techlibs 是「编译期安装、运行期按路径读取」的数据资源。

#### 4.1.2 核心流程

techlibs 从源码到生效的链路:

1. **构建期**:CMake 遍历 `techlibs/CMakeLists.txt`,对每个厂商目录 `add_subdirectory`。
2. **声明资源**:每个 pass(如 `synth`、`synth_xilinx`)在自己的 `yosys_pass(... DATA_FILES ...)` 里列出它依赖的 `.v` 文件。
3. **安装资源**:CMake 把这些 `DATA_FILES` 复制/安装到 share dir,保持相对目录结构。
4. **运行期**:脚本(或 ScriptPass)用 `+/相对路径` 引用这些文件,经 `read_verilog` 或 `techmap -map` 读入。

`+/` 不是 shell 路径,而是 yosys 内部的「share dir 前缀」占位符,由 `Pass::frontend_call` / `read_verilog` 在解析时展开为实际安装路径。

#### 4.1.3 源码精读

techlibs 顶层入口按字母序列出所有厂商目录,每个目录就是一个目标平台:

[techlibs/CMakeLists.txt:1-21](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/CMakeLists.txt#L1-L21) — 二十个 `add_subdirectory`,从 achronix 到 xilinx,加上一个公共的 `common`。可以看到 yosys 支持的厂商广度。

那么这些 `.v` 文件如何被声明为「资源」?看 common 的 CMakeLists:

[techlibs/common/CMakeLists.txt:30-49](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/CMakeLists.txt#L30-L49) — `yosys_pass(synth ... DATA_FILES simlib.v simcells.v techmap.v ...)`。注意:这些库被挂在 **`synth` 这个 pass 名下**作为它的数据依赖,意味着只要构建了 synth,这些 `.v` 就会被安装。`abc9_model.v`、`mul2dsp.v`、`choices/*.v` 等也都是同理安装的资源。

techmap pass 在没有显式 `-map` 时,默认就去 share dir 找这个安装好的 `techmap.v`:

[passes/techmap/techmap.cc:1206-1208](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L1206-L1208) — `if (map_files.empty()) Frontend::frontend_call(map, nullptr, "+/techmap.v", verilog_frontend);`。这里的 `+/techmap.v` 正是引用第 3 步安装的 common 公共库。这就是为什么你写 `techmap` 不带任何参数也能工作——它有默认库。

#### 4.1.4 代码实践

**实践目标**:验证「techlibs 是数据、按 `DATA_FILES` 安装」这一论断。

**操作步骤**:

1. 在 `techlibs/common/CMakeLists.txt` 里找到 `synth` 的 `DATA_FILES` 列表,数一数列了多少个 `.v`。
2. 在 `techlibs/xilinx/CMakeLists.txt` 里找到 `synth_xilinx` 的 `DATA_FILES`,对比它多出了哪些厂商专属文件(如 `brams_*_map.v`、`*_dsp_map.v`)。
3. 在 `passes/techmap/techmap.cc` 中搜索 `"+/techmap.v"`,确认默认库路径。

**需要观察的现象**:xilinx 的 `DATA_FILES` 远多于 common,因为 FPGA 综合需要 BRAM/DSP/IO 等大量专属映射资源;而 common 只需通用综合的最小集合。

**预期结果**:你会直观感受到「平台越复杂,techlibs 子目录越庞大」——这也是为什么后面 [u8-l2](u8-l2-vendor-synth-flows.md) 要专门讲厂商流程。

#### 4.1.5 小练习与答案

**练习 1**:为什么 techlibs 里的文件是 `.v` 而不是 `.cc`?
**答**:因为它们是 techmap/read_verilog 在运行时读取的**数据资源**(映射模板、仿真模型、黑盒库),用 Verilog 编写才能被前端解析、被 techmap 当模板内联;写成 C++ 就失去了「数据驱动、免重编译即可新增映射」的优势(回顾 [u6-l5](u6-l5-techmap-simplemap.md) 的设计哲学)。

**练习 2**:`+/xilinx/cells_sim.v` 中的 `+/` 指向哪里?
**答**:指向 yosys 的共享数据目录(share dir)根,即 `DATA_FILES` 安装后的位置;`xilinx/cells_sim.v` 是其中的相对路径。

---

### 4.2 cells_sim.v 与 cells_map.v 的分工(`_sim` / `_map` 后缀)

#### 4.2.1 概念说明

这是本讲最核心的辨析。techlibs 里最常见的两种文件名后缀,对应两种完全不同的用途:

- **`cells_sim.v`** = simulation,描述单元的**功能行为**(有时含定时)。它回答「这个单元逻辑上是什么」。在 common 里它描述 yosys 内部门级 `$_` 单元;在厂商目录里它描述目标芯片的**真实硅单元**(如 Xilinx 的 LUT6、FDRE)。
- **`cells_map.v`** = mapping,描述**如何用 techmap 把通用 cell 替换成目标单元**。它回答「怎么变过去」。它的标志是模板里出现 `_TECHMAP_REPLACE_`、`techmap_celltype` 等 techmap 专用属性。

一个关键事实:**common 目录只有 `techmap.v` 作为映射库,没有 `cells_map.v`**;而厂商目录(如 xilinx)则是 `cells_sim.v` + `cells_map.v`(以及 `lut_map.v`/`ff_map.v` 等一串 `*_map.v`)成对出现。原因是:common 的 `techmap.v` 是最基础、最通用的「`$ → $_`」映射器,地位特殊所以用了专有名 `techmap.v`;厂商继承 common 的 `techmap.v` 做通用下沉,自己只需补充「把 `$_`/`$lut` 变成真实原语」的目标专属映射,于是拆成多个 `*_map.v`。

#### 4.2.2 核心流程

一个厂商单元从「通用」到「真实原语」的两步走:

```
高层 $ 单元  ──techmap -map +/techmap.v──▶  门级 $_ 单元  ──techmap -map +/厂商/*_map.v──▶  真实原语
($not/$and)        (common 通用映射)        ($_NOT_/$_AND_)     (厂商专属映射)            (LUT6/FDRE)
```

- 第一步用 common 的 `techmap.v`,把位宽可变的高层 `$` 单元降到单位宽门级 `$_` 单元(详见 [u6-l5](u6-l5-techmap-simplemap.md))。
- 第二步用厂商的 `*_map.v`,把 `$_`/`$lut` 等替换成芯片真实原语。
- 而 `cells_sim.v` 贯穿全程,作为「这些原语功能上是什么」的参考——在 begin 阶段以 `-lib` 加载,让 yosys 认识原语名;综合后若做仿真,这些模型又能描述其行为。

#### 4.2.3 源码精读

先看 common 的两个 sim 库如何自我说明。`simcells.v` 头注释明确指出它是「内部门级单元的仿真库,由默认 techmap 生成、abc 期望」:

[techlibs/common/simcells.v:20-26](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simcells.v#L20-L26) — "simple simulation models for the internal logic cells (`$_NOT_`, `$_AND_`, ...) that are generated by the default technology mapper (see 'techmap.v') and expected by the 'abc' pass"。这句话点明了 simcells.v 与 techmap.v、abc 三者的关系:techmap.v 产出 `$_` 单元,abc 消费它们,simcells.v 给它们提供仿真模型。

它的模块全是单位宽、无参数的内部门:

[techlibs/common/simcells.v:58-62](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simcells.v#L58-L62) — `$_NOT_` 用一行 `assign Y = ~A;` 描述。注意它没有 parameter,因为门级单元就是单 bit。

对照看厂商的 `cells_sim.v`——它描述的是**真实 Xilinx 原语**,而且带定时:

[techlibs/xilinx/cells_sim.v:258-274](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_sim.v#L258-L274) — `LUT6` 模型:用 64 位 `INIT` 参数 + 一串 `wire` 选择链模拟 6 输入查找表,并带 `specify` 定时块(如 `(I0 => O) = 642`)。这是真实硅单元的行为+定时模型,文件头还引用了 Xilinx 官方手册:

[techlibs/xilinx/cells_sim.v:20-22](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_sim.v#L20-L22) — "See Xilinx UG953 and UG474 for a description of the cell types below"。

再看厂商的 `cells_map.v`——这才是「映射模板」,核心是 `_TECHMAP_REPLACE_`:

[techlibs/xilinx/cells_map.v:21-28](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/cells_map.v#L21-L28) — 模块 `$__SHREG_` 内部用 `$__XILINX_SHREG_ #(...) _TECHMAP_REPLACE_ (.C(C),...)` 把自己替换成 Xilinx 的移位寄存器实现。`_TECHMAP_REPLACE_` 是 techmap 的「替换占位符」(回顾 [u6-l5](u6-l5-techmap-simplemap.md)),它的存在就是 `_map` 文件的身份证。

`lut_map.v` 进一步展示「按参数分派」的映射,把通用 `$lut` 变成 LUT1..LUT6:

[techlibs/xilinx/lut_map.v:25-64](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/lut_map.v#L25-L64) — `module $lut` 用 `generate` 按 `WIDTH` 选择:WIDTH=1 时可化简成 `INV` 或 `LUT1`,WIDTH=6 时例化 `LUT6`,全部以 `_TECHMAP_REPLACE_` 收口。当宽度超出单片 LUT 容量时还会级联 `MUXF5`(第 65 行之后),并用 `_TECHMAP_FAIL_` 表示无法映射的情况。

#### 4.2.4 代码实践

**实践目标**:对比 common 与 xilinx 两个 `cells_sim.v`,体会「同一后缀、不同抽象层」。

**操作步骤**:

1. 打开 `techlibs/common/simcells.v`,统计其中以 `$_` 开头的模块数量(可用 `grep -c '^module \\$_'`)。
2. 打开 `techlibs/xilinx/cells_sim.v`,挑出几个代表模块名:`LUT6`、`FDRE`、`CARRY4`、`IBUF`、`BUFG`。
3. 对比两者的「参数化程度」:common 的 `$_AND_` 是否有 parameter?xilinx 的 `LUT6` 是否有 `INIT`?

**需要观察的现象**:common 的 simcells 是 149 个单 bit 内部门(无参数,纯逻辑);xilinx 的 cells_sim 是 99 个真实原语(带 `INIT` 等参数、带 `specify` 定时、含 IO/时钟缓冲等芯片专属单元)。

**预期结果**:你会得出结论——`_sim` 后缀在 common 与 vendor 两处语义一致(都是「仿真模型」),但抽象层不同:common 仿真的是 yosys 内部表示,vendor 仿真的是目标硅片。

> 若本地已构建 yosys,可运行 `yosys -p "read_verilog -lib +/xilinx/cells_sim.v; select LUT6; show"` 直观查看一个 LUT6 模型。若未构建,则标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**:为什么 common 目录没有 `cells_map.v`,只有一个 `techmap.v`?
**答**:`techmap.v` 就是 common 的映射库,它做最通用的「`$ → $_`」下沉;因为它是所有厂商共享的基础设施,地位特殊,所以用了不带 `_map` 后缀的专有名。厂商不需要再写通用 `$→$_` 映射(继承即可),只需补充目标专属映射,因而拆成 `cells_map.v`、`lut_map.v`、`ff_map.v` 等。

**练习 2**:同一个 `xilinx/cells_sim.v` 既能被 `read_verilog -lib` 加载,又能(在概念上)作为参考——这两种「身份」的区别是什么?
**答**:`-lib` 把模块当**黑盒**(只保留端口、丢弃内部),目的是让 yosys「认识」原语名以便后续映射;而它作为仿真模型时是**白盒**(保留 `assign`/`always`/`specify`),用于描述功能与定时。同一文件,加载方式决定身份。

**练习 3**:`_map` 文件最可靠的「身份证」是什么?
**答**:模板里出现 techmap 专用属性,尤其是 `_TECHMAP_REPLACE_`(替换占位)、`techmap_celltype`(声明匹配的 cell 类型)、`_TECHMAP_FAIL_`(条件退出)、`_TECHMAP_DO_*`(驱动子 pass)。`_sim` 文件则只有普通 `assign`/`always`,不带这些属性。

---

### 4.3 common 公共库三件套:simlib / simcells / techmap.v

#### 4.3.1 概念说明

common 目录里有三个最重要的 `.v` 文件,它们构成 yosys 内置综合的「标准件三件套」,恰好对应上一讲的三个抽象层:

| 文件 | 描述的对象 | 抽象层 | 角色 |
|---|---|---|---|
| `simlib.v` | 高层 `$` 单元(`$not`、`$and`、`$mul`...) | 最高(位宽可变、行为级) | 仿真模型 |
| `techmap.v` | `$ → $_` 的映射规则 | 中间(改写规则) | 映射模板 |
| `simcells.v` | 门级 `$_` 单元(`$_NOT_`、`$_AND_`...) | 最低(单位宽门级) | 仿真模型 |

三者形成一条链:**前端/Pass 产出高层 `$` 单元** →(用 `simlib.v` 可仿真验证)→ **`techmap.v` 把 `$` 降到 `$_`** →(用 `simcells.v` 可仿真验证)→ **abc/dfflibmap 把 `$_` 映射到工艺**。注意 `simlib.v` 和 `simcells.v` 都是「仿真模型」(都带 `_sim` 的语义,只是 simlib 没用该后缀),分别覆盖高层与门级两个层次;`techmap.v` 是连接两者的映射器。

#### 4.3.2 核心流程

`simlib.v` 的模块特征:**位宽参数化 + 行为级描述**。每个高层 `$` 单元都带 `A_WIDTH`/`B_WIDTH`/`Y_WIDTH`/`A_SIGNED` 等参数,用 `generate` 区分有无符号,用一条 `assign` 表达行为。例如一个 `$not` 可以是 32 位,也可以是 1 位——位宽是参数,不是固定结构。

`simcells.v` 的模块特征:**单位宽 + 无参数 + 纯组合/时序原语**。每个 `$_` 单元就是 1 bit 的门,极性(上升/下降沿、高/低有效复位)直接编进名字(如 `$_DFF_P_` 表示上升沿、`$_DFF_N_` 表示下降沿)。这一点在 [u3-l4 内部单元库](u3-l4-internal-cell-library.md) 已建立认知:门级 `$_` 单元把极性编进名字。

`techmap.v` 的模块特征:**声明式 + 委托式**。它大量使用 `techmap_celltype` 声明「我匹配哪些 `$` 类型」,用 `techmap_simplemap` 把机械单元委托给 simplemap,用 `_TECHMAP_DO_*` 驱动 `proc`/`opt` 自动门级化行为级模板。

#### 4.3.3 源码精读

`simlib.v` 头注释点明它服务于「前端产出、多数 pass 使用」的高层 `$` 单元:

[techlibs/common/simlib.v:20-32](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simlib.v#L20-L32) — "simple simulation models for the internal cells (`$not`, ...) generated by the frontends and used in most passes. This library can be used to verify the internal netlists"。

看一个典型高层模型 `$not`,体会「参数化 + 行为级」:

[techlibs/common/simlib.v:48-65](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simlib.v#L48-L65) — 带三个 parameter(`A_SIGNED`/`A_WIDTH`/`Y_WIDTH`),用 `generate if (A_SIGNED)` 分有符号/无符号两路,输出 `assign Y = ~...`。位宽完全由参数决定,这就是「高层单元」的标志。

再看 `techmap.v` 头注释,点明它是「`$ → $_`」的映射器,且明确不处理 `$mem`(要先用 memory_map):

[techlibs/common/techmap.v:20-31](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L20-L31) — "the mapping of internal cells (e.g. `$not` with variable bit width) to the internal logic cells (such as the single bit `$_NOT_` gate)... does not map `$mem` cells"。

`techmap.v` 开篇是一组「委托给 simplemap」的声明,体现「数据驱动 + 委托」设计:

[techlibs/common/techmap.v:41-69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L41-L69) — 用 `(* techmap_simplemap *)` + `(* techmap_celltype = "$not $and $or ..." *)` 把一堆机械单元(布尔运算、归约运算、比较、多路、寄存器)整体委托给 simplemap 快速通道(回顾 [u6-l5](u6-l5-techmap-simplemap.md):simplemap 是写死在 C++ 里的逐位分解)。注意这些模块体是**空的**(`endmodule`),它们只是「声明」,真正的展开由 simplemap 完成。

对于 simplemap 处理不了的(如移位),techmap.v 写真正的行为级模板,并用 `_TECHMAP_DO_*` 驱动子 pass 把行为级自动门级化:

[techlibs/common/techmap.v:76-122](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L76-L122) — 移位运算模板 `_90_shift_ops_shr_shl_sshl_sshr`:用 `_TECHMAP_CELLTYPE_` 判断具体是哪种移位,用 `wire [1023:0] _TECHMAP_DO_00_ = "proc;;"` 与 `_TECHMAP_DO_01_ = "RECURSION; CONSTMAP; opt_muxtree; opt_expr ..."` 告诉 techmap「内联我之后,依次跑这些 pass 把我的 always 块门级化」。这是 techmap 模板的精髓:写行为级 Verilog,让现有 pass 帮你降到门级。

最后,`simcells.v` 里大量触发器类型是自动生成的,头部有明确警告:

[techlibs/common/simcells.v:479-482](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simcells.v#L479-L482) — "the following cell types are autogenerated. DO NOT EDIT them manually, instead edit the templates in gen_ff_types.py and rerun it"。这说明 common 库部分由脚本(`gen_ff_types.py`)从模板生成,以覆盖边沿/使能/复位/置位极性的全部组合(`$_DFFE_PP0P_` 这类长名字正是组合枚举的产物)。

#### 4.3.4 代码实践

**实践目标**:从源码确认三件套的「抽象层递降」关系。

**操作步骤**:

1. 在 `simlib.v` 中找到 `$not`(第 48 行),记录它的 parameter 列表与 `generate` 结构。
2. 在 `techmap.v` 中找到把 `$not` 委托给 simplemap 的声明(第 41-44 行的 `techmap_celltype = "$not $and ..."`),确认 `$not` 在 `_90_simplemap_bool_ops` 组里。
3. 在 `simcells.v` 中找到 `$_NOT_`(第 58 行),确认它是单 bit、无参数。
4. 画出三者的箭头链:`$not`(simlib)──techmap.v──▶ `$_NOT_`(simcells)。

**需要观察的现象**:`$not` 有 3 个参数、可任意位宽;`$_NOT_` 无参数、固定 1 bit;techmap.v 用一行 `techmap_celltype` 把前者归入 simplemap 组,由 simplemap 做逐位展开。

**预期结果**:你将直观看到「同一种逻辑(取反)在三个抽象层的三种写法」,这是理解 yosys 内部表示层次的关键样本。

#### 4.3.5 小练习与答案

**练习 1**:`simlib.v` 和 `simcells.v` 都是仿真模型,为什么需要两份?
**答**:它们仿真的是**不同抽象层**的同一设计。`simlib.v` 仿真前端刚产出的高层 `$` 单元(位宽可变、行为级),用于在综合早期验证;`simcells.v` 仿真 techmap 产出的门级 `$_` 单元(单位宽),用于在综合后期、abc 之前验证。两份模型让设计在「降到门级」前后都能被仿真检查。

**练习 2**:`techmap.v` 里那些 `endmodule` 为空的模块(如 `_90_simplemap_bool_ops`)到底做了什么?
**答**:它们本身没有逻辑,只是用 `(* techmap_simplemap *)` + `(* techmap_celltype = "..." *)` 属性声明「这些 `$` 类型交给 simplemap 处理」。techmap 匹配到这些类型后,转而调用 simplemap 的 C++ 快速通道做逐位展开,而非用模板内联。

**练习 3**:为什么 `techmap.v` 头注释特意说「不映射 `$mem`」?
**答**:因为存储器有专门的 `memory_map` pass 负责(回顾 [u6-l4 memory](u6-l4-memory.md)),把 `$mem` 拆成 `$dff` + `$mux`。techmap.v 只处理「逻辑/运算/寄存器」类单元,存储器要先经 memory_map 变成这些单元后,techmap 才接得上。

---

### 4.4 techlibs 与 techmap pass 的协作

#### 4.4.1 概念说明

前三节讲了 techlibs「是什么」,本节讲它「怎么被用起来」。核心是 techmap pass 与 `read_verilog` 如何通过 `+/` 路径把 techlibs 的 `.v` 文件读进设计。这里有三条加载路径要分清:

1. **`techmap -map +/xxx.v`**:把 `xxx.v` 当**映射模板库**,匹配并替换当前设计里的 cell。
2. **`read_verilog -lib +/xxx.v`**:把 `xxx.v` 当**黑盒库**,每个模块只留端口,让 yosys 认识这些原语名。
3. **`read_verilog -lib -specify +/xxx.v`**:同上,但额外保留 `specify` 定时块(供时序分析/abc 使用)。

厂商 ScriptPass(如 `synth_xilinx`)就是按固定顺序组合这三条路径,把 techlibs 资源串成完整流程。

#### 4.4.2 核心流程

以 `synth_xilinx` 为例,它对 techlibs 资源的典型使用顺序:

1. **begin 阶段**:用 `read_verilog -lib -specify +/xilinx/cells_sim.v` 预先声明所有 Xilinx 原语(带定时),让后续映射「有目标可对」。
2. **通用下沉**:用 `techmap -map +/techmap.v`(可能加 `-D LUT_SIZE=`)把高层 `$` 单元降到门级 `$_`/`$lut`。
3. **厂商映射**:用 `techmap -map +/xilinx/cells_map.v`(及 `lut_map.v`、`ff_map.v`)把门级单元替换成 Xilinx 真实原语。

这三步对应了 4.2.2 节那张流程图,只是现在落地到了具体的 `+/` 路径上。

#### 4.4.3 源码精读

`synth_xilinx` 在 begin 阶段加载厂商仿真库(作为带定时的黑盒):

[techlibs/xilinx/synth_xilinx.cc:346-351](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L346-L351) — `read_args += " -lib -specify +/xilinx/cells_sim.v";` 紧跟 `run("read_verilog" + read_args);`,再 `read_verilog -lib +/xilinx/cells_xtra.v`。`-lib` 让 LUT6/FDRE 等以黑盒形式注册,`-specify` 保留它们的定时信息。

随后用 common 的 techmap.v 做通用下沉(注意 `-D LUT_SIZE` 传参给模板):

[techlibs/xilinx/synth_xilinx.cc:602](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L602) 与 [techlibs/xilinx/synth_xilinx.cc:619](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L619) — `techmap ... -map +/techmap.v -D LUT_SIZE=...` 与 `-map +/techmap.v -map +/xilinx/cells_map.v`。可以看到一次 techmap 调用可以叠加多个 `-map`(common 的 + 厂商的),techmap 会合并所有模板库一起匹配。

最后用厂商的 LUT 映射把 `$lut` 变成真实 LUT 原语:

[techlibs/xilinx/synth_xilinx.cc:698](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/xilinx/synth_xilinx.cc#L698) — `techmap -map +/xilinx/lut_map.v -map +/xilinx/cells_map.v`。这条命令同时挂载 `lut_map.v`(`$lut → LUT1..6`)和 `cells_map.v`(其它 vendor 替换),把剩余通用单元全部落到 Xilinx 原语。

把这些命令与 4.3 节的三件套对照,就形成了完整的资源使用闭环:`cells_sim.v`(声明原语)+ `techmap.v`(通用下沉)+ `cells_map.v`/`lut_map.v`(厂商映射)。

#### 4.4.4 代码实践

**实践目标**:跟踪 `synth_xilinx` 对 techlibs 资源的完整引用链。

**操作步骤**:

1. 打开 `techlibs/xilinx/synth_xilinx.cc`,搜索所有 `+/` 开头的字符串,按出现顺序列出。
2. 把它们分成三类:`-lib`/`-specify` 加载的黑盒库、`techmap -map` 的通用库、`techmap -map` 的厂商库。
3. 对照 `techlibs/xilinx/CMakeLists.txt` 的 `DATA_FILES`,确认这些 `+/xilinx/...` 文件确实都被声明安装。

**需要观察的现象**:`synth_xilinx` 引用的 `+/xilinx/cells_sim.v`、`cells_map.v`、`lut_map.v`、`ff_map.v` 等,全部出现在该目录 `DATA_FILES` 列表中;而 `+/techmap.v`(不带 `xilinx/`)来自 common。

**预期结果**:你会确认「ScriptPass 里的 `+/` 引用」与「CMakeLists 的 `DATA_FILES`」是一一对应的安装-引用关系——没有 `DATA_FILES` 声明的文件,运行时 `+/` 就找不到。

> 若本地已构建,可运行 `yosys -p "read_verilog -lib +/xilinx/cells_sim.v; stat"` 观察 yosys 读入了多少个 Xilinx 黑盒模块。若未构建,标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**:`techmap -map +/techmap.v -map +/xilinx/cells_map.v` 里两个 `-map` 是什么关系?
**答**:叠加关系。techmap 把所有 `-map` 指定的模板库合并成一个映射设计,一起参与匹配替换。这样一次调用就能同时应用「common 通用规则」和「xilinx 厂商规则」,不必分两次。

**练习 2**:如果某个 `.v` 文件没有被任何 `DATA_FILES` 声明,运行时用 `+/` 引用它会发生什么?
**答**:该文件不会被安装到 share dir,`+/` 路径解析时找不到文件,`read_verilog`/`techmap` 会报错「can't open」。这正是 CMakeLists 的 `DATA_FILES` 机制存在的意义——它是安装的授权清单。

**练习 3**:为什么 begin 阶段加载 `cells_sim.v` 用 `-lib`,而不是普通 `read_verilog`?
**答**:因为此时只是要 yosys「认识」Xilinx 原语名并保留定时,不需要(也不应该)把这些原语的内部行为内联进设计(它们是要被综合成的目标,不是待综合的源)。`-lib` 以黑盒方式注册,正符合「目标原语声明」的语义。

---

## 5. 综合实践

**任务**:用源码阅读 + (可选)运行,亲手验证 techlibs 的「数据-安装-引用」闭环,并解释 `_sim`/`_map` 后缀。

**步骤**:

1. **定位资源清单**:打开 `techlibs/common/CMakeLists.txt` 与 `techlibs/xilinx/CMakeLists.txt`,分别列出 `synth` 与 `synth_xilinx` 的 `DATA_FILES`。观察 xilinx 比 common 多出的厂商专属资源类别(BRAM、DSP、IO、LUTRAM 等)。

2. **对比两个 `cells_sim.v`**:
   - common 的 `simcells.v`:确认其模块以 `$_` 开头、无参数、单位宽(如 `$_AND_` 第 78 行)。统计模块数(约 149)。
   - xilinx 的 `cells_sim.v`:确认其模块是真实原语(如 `LUT6` 第 258 行带 `INIT` 参数与 `specify`)。统计模块数(约 99)。
   - 写一段话说明:两者都叫 `cells_sim.v`、都是仿真模型,但 common 仿真的是 yosys **内部门级表示**,xilinx 仿真的是**目标硅片原语**。

3. **解释后缀**:打开 `techlibs/xilinx/cells_map.v` 与 `lut_map.v`,指出其中的 `_TECHMAP_REPLACE_`、`techmap_celltype`、`_TECHMAP_FAIL_`。据此说明:`_map` 文件 = 含 techmap 替换属性的映射模板;`_sim` 文件 = 纯行为描述、不含这些属性。

4. **跟踪引用链**:在 `synth_xilinx.cc` 中找到对 `+/xilinx/cells_sim.v`(第 348 行)、`+/techmap.v`(第 602、619 行)、`+/xilinx/lut_map.v`(第 698 行)的引用,说明它们分别对应「声明原语」「通用下沉」「厂商映射」三步。

5. **(可选)运行验证**(待本地验证):若已构建 yosys,准备一个最小 Verilog(如 `module top(input a,b,output y); assign y = a & b; endmodule`),运行:

   ```
   yosys -p "read_verilog top.v; synth_xilinx -family xc7; stat"
   ```

   观察 `stat` 输出中是否出现 `LUT2`/`LUT6` 等 Xilinx 原语(而非 `$_AND_`),以此验证门级单元确实经 `*_map.v` 变成了真实原语。

**预期结果**:你将能用一张表说清「common 三件套 + 厂商 cells_sim/cells_map」各自的角色、抽象层与加载方式,并能解释为什么 `_sim`/`_map` 是「功能描述 vs 映射规则」之分,而非「common vs vendor」之分。

## 6. 本讲小结

- `techlibs/` 是**数据资源仓库**而非代码:按厂商分目录,`.v` 文件经 CMake 的 `DATA_FILES` 安装到 share dir,运行时用 `+/` 路径引用。
- `_sim` 后缀 = **仿真模型**(描述功能/定时);`_map` 后缀 = **映射模板**(含 `_TECHMAP_REPLACE_` 等 techmap 属性)。这是「功能 vs 规则」之分。
- common 公共库三件套:`simlib.v`(高层 `$` 仿真)→ `techmap.v`(`$ → $_` 通用映射,默认库)→ `simcells.v`(门级 `$_` 仿真),构成抽象层递降的一条链。
- 同一 `cells_sim.v` 可在不同抽象层出现:common 仿真内部门(`$_AND_`),vendor 仿真真实硅片(LUT6);common 没有 `cells_map.v`,因为 `techmap.v` 就是它的通用映射库。
- 加载三路径:`techmap -map`(模板)、`read_verilog -lib`(黑盒)、`read_verilog -lib -specify`(黑盒+定时),厂商 ScriptPass 按序组合它们。
- techmap 无 `-map` 时默认加载 `+/techmap.v`;`DATA_FILES` 是文件能被 `+/` 找到的「安装授权」。

## 7. 下一步学习建议

- **下一讲 [u8-l2 目标平台综合流程:synth_xilinx / synth_ice40](u8-l2-vendor-synth-flows.md)**:本讲只看了 techlibs「静态结构」,下一讲将动态跟踪 `synth_xilinx`/`synth_ice40` 这些厂商 ScriptPass 如何在通用 synth 基础上插入 LUT/BRAM/DSP 映射阶段,把本讲的 `cells_map.v`、`lut_map.v`、`brams_*_map.v` 真正串成流程。
- **回看 [u6-l5 techmap 与 simplemap](u6-l5-techmap-simplemap.md)**:本讲大量引用 `_TECHMAP_REPLACE_`、`techmap_celltype`、`_TECHMAP_DO_*`、`_TECHMAP_FAIL_`,若对它们的运行机制不熟,建议回看该讲「模板靠属性驱动 techmap」一节。
- **延伸阅读源码**:想了解 BRAM/DSP 这类厂商专属资源如何描述,可阅读 `techlibs/xilinx/brams_xcu_map.v` 与 `xcu_dsp_map.v`,它们是把 `$mem`/`$mul` 映射到硬件宏的映射模板,结构与 `lut_map.v` 同构。
- **动手验证**:若已构建 yosys,用 `yosys -p "help techmap"` 与 `yosys -p "help synth_xilinx"` 查看命令帮助,对照本讲的 `+/` 引用,加深对「资源-安装-引用」闭环的印象。
