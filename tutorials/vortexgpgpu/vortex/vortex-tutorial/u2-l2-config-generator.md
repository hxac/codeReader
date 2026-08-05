# gen_config.py 与配置值流

## 1. 本讲目标

在 u2-l1 里我们确立了一条铁律：**所有配置常量只活在两个 TOML 文件里**（`VX_config.toml` 与 `VX_types.toml`），任何代码都不得手写它们。本讲要回答紧接着的下一个问题——

> 这些写在 TOML 里的值，是怎么「长出脚」走到 RTL、仿真器、运行时、内核和测试代码里去的？

答案是唯一的：一个叫 [`ci/gen_config.py`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py) 的 Python 生成器。学完本讲你应该能够：

- 说清 `gen_config.py` 把一个 TOML 解析成 **cflags / cpp / verilog 三种输出格式**时，分别产出什么、给谁用。
- 画出一条完整的「值流」：TOML → 生成器 → `.h`/`.vh`/`-D` 标志 → 各层消费者，并区分 **configure 时刻**与 **make 时刻**两次驱动。
- 区分 **resolved（已求值）** 与 **unresolved（未求值）** 两种解析模式，理解为什么同一种格式会有两副面孔。
- 读懂 `expr:` 表达式语言，以及 `[[builtin]]` / `[[enum]]` / `[[param]]` / 小写私有变量这四类符号的解析规则。
- 会手动运行 `gen_config.py`，并对同一参数在三种格式下的不同表示做 diff。

## 2. 前置知识

本讲默认你已经读过 u2-l1，建立了以下认知（这里只做一句话回顾，不展开）：

- **单一真相来源**：`VX_config.toml`（约 245 键，HW/sim 私有的硬件构建配置）与 `VX_types.toml`（软硬件共享的 ISA/ABI 契约）。
- **`VX_CFG_*` 命名空间**：配置旋钮的名字直接写在 TOML 键名里，生成器本身不做任何命名逻辑。
- **`_ENABLE` 与 `_ENABLED`**：手写布尔 `_ENABLE`，自动派生整数镜像 `_ENABLED`，两者不漂移。
- **configure 是模板填空机**：用 `sed` 烘焙 `@占位符@`，并调用 `gen_config.py` 生成 `hw/*.vh` 与 `sw/*.h`（见 u1-l3）。

如果你还记得 u1-l4 里 `--cores=2` 会被翻译成 `-DVX_CFG_NUM_CORES=2`，那么本讲就是在解释「翻译」二字的内部机制。

另外需要两个最基础的概念：

- **预处理器宏（`#define` / `` `define ``）**：C 用 `#ifndef`/`#define`/`#endif` 守卫一个宏是否已被定义；SystemVerilog 用反引号版本 `` `ifndef ``/`` `define ``/`` `endif ``。本讲会大量出现这两种「方言」。
- **`-DNAME=value`**：编译器命令行参数，等价于在源文件最前面 `#define NAME value`。`-DNAME`（不带值）等价于 `#define NAME`（定义为「空」，常作布尔开关用）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`ci/gen_config.py`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py) | 配置生成器本体：读一个 TOML，按 `cflags`/`cpp`/`verilog` 三种格式之一，输出宏定义。本讲的主角。 |
| [`configure`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure) | 源外构建的入口脚本。它在 configure 时刻遍历 `*.toml`，调用 `gen_config.py` 生成 `hw/*.vh` 与 `sw/*.h`。 |
| [`docs/designs/build_configuration_system.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md) | 配置系统的设计文档。本讲重点对照它的 §2「值流图」。 |
| [`VX_config.toml`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml) | 硬件构建配置（HW/sim 私有）。本讲拿它当生成器的输入样例。 |
| [`VX_types.toml`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml) | ISA/ABI 契约（软硬件共享），含 `[[builtin]] XLEN`。 |
| [`tests/regression/common.mk`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/common.mk) | 内核/测试构建的公共 Makefile 片段。它在 **make 时刻**调用 `gen_config.py --format cflags`，是「值流」的第二条腿。 |
| [`hw/rtl/VX_define.vh`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh) | RTL 侧全局头文件，`include` 了生成器产出的 `VX_config.vh` 与 `VX_types.vh`。 |

## 4. 核心概念与源码讲解

### 4.1 生成器全景：一个 toml 与三种输出格式

#### 4.1.1 概念说明

`gen_config.py` 是一台**纯粹的「TOML → 宏定义」翻译机**。它的命令行接口极其简单（见 [gen_config.py:L1485-L1492](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1485-L1492)）：

```
--config / -c   <一个 toml 文件>          # 必填，输入
--format  / -f  {cflags, cpp, verilog}    # 输出格式，默认 cflags
--output  / -o  <输出文件>                 # 不给就打到 stdout
--resolved / -r                            # 对 cpp/verilog 强制求值
--cflags       <已有的 -D 标志串>          # 用作覆盖来源
```

三种格式面向三类不同的消费者：

- **`cflags`**：输出一串 `-DNAME=value` 编译标志，供 **sim、kernel、test** 的 C/C++ 构建直接吃进 `CFLAGS`。
- **`cpp`**：输出一个 C 头文件（`#ifndef`/`#define`/`#endif`），落到 `build/sw/<name>.h`，供 **runtime/sim** include。
- **`verilog`**：输出一个 SystemVerilog 头文件（`` `ifndef ``/`` `define ``），落到 `build/hw/<name>.vh`，供 **RTL** include。

关键点：**同一个 toml、同一批值，在三种格式下只是「换了一套语法外壳」**，语义完全一致。这正是「单一真相来源」能成立的工程基础——值只写一遍，外壳由生成器自动套。

#### 4.1.2 核心流程

`main()` 决定走哪条生成路径的逻辑只有一行（[gen_config.py:L1526](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1526)）：

```python
resolved = True if args.format == "cflags" else args.resolved
```

由此分出三条路：

```
        ┌── format=cflags ─────► emit_cflags()          （永远 resolved）
toml ──►├── format=cpp    ──┬── resolved    ─► emit_resolved_header("cpp")
        │                    └── 默认(unresolved) ─► emit_unresolved_header("cpp")
        └── format=verilog ─┬── resolved    ─► emit_resolved_header("verilog")
                             └── 默认(unresolved) ─► emit_unresolved_header("verilog")
```

也就是说：**`cflags` 永远是 resolved（求值后的常数）**；而 `cpp`/`verilog` 默认是 unresolved（保留 `ifndef` 守卫，可被 `-D` 覆盖），只有显式加 `--resolved` 才会求值成常数。这两个模式的区别是 4.3 节的主题。

#### 4.1.3 源码精读

三个输出函数分别是：

- [`emit_cflags`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1449-L1477)：遍历已求值的 `cfg` 字典，把每个键拼成 `-D` 标志。布尔为真就输出 `-DKEY`，整数输出 `-DKEY=value`，并自动追加 `_ENABLE` 的 `_ENABLED` 整数镜像。
- [`emit_resolved_header`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1394-L1446)：输出 `#define KEY value` 形式的常数，带文件头注释与 include guard。
- [`emit_unresolved_header`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1170-L1224)：输出带 `ifndef` 守卫的「可覆盖」宏，遇到 `expr:` 条件还会展开成嵌套 `ifdef` 树。

以一个普通整数 `VX_CFG_NUM_WARPS = 4`（[VX_config.toml:L43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L43)）为例，根据源码逻辑（非运行结果，下同），三种格式的输出形如：

| 格式 | 输出（由源码逻辑推导） |
| --- | --- |
| cflags | `-DVX_CFG_NUM_WARPS=4` |
| cpp（resolved） | `#define VX_CFG_NUM_WARPS 4` |
| cpp（unresolved） | `#ifndef VX_CFG_NUM_WARPS`<br>`#define VX_CFG_NUM_WARPS 4`<br>`#endif` |
| verilog（unresolved） | `` `ifndef VX_CFG_NUM_WARPS ``<br>`` `define VX_CFG_NUM_WARPS 4 ``<br>`` `endif `` |

可以看到：cflags 是「扁平的标志串」；resolved 头是「裸常数」；unresolved 头是「带守卫、可被 `-D` 抢先定义覆盖」的宏。**同一个值，三种语法外壳**。

#### 4.1.4 代码实践

**目标**：亲手把 `VX_config.toml` 喂给生成器，拿到三种格式的真实输出。

**操作步骤**（在仓库根目录）：

```bash
# 1) cflags：注意必须给 -DVX_CFG_XLEN，原因见 4.3/4.4
python3 ci/gen_config.py -c VX_config.toml -f cflags --cflags='-DVX_CFG_XLEN=32' > /tmp/c.txt
# 2) cpp 头（默认 unresolved，即 configure 实际生成的形态）
python3 ci/gen_config.py -c VX_config.toml -f cpp                 > /tmp/cpp.txt
# 3) verilog 头（默认 unresolved）
python3 ci/gen_config.py -c VX_config.toml -f verilog             > /tmp/vh.txt
```

**需要观察的现象**：

- `c.txt` 是一长串单行 `-D...` 标志；`cpp.txt` / `vh.txt` 是多行带 `ifndef` 守卫的头文件。
- 在 `cpp.txt` 里 `grep VX_CFG_NUM_WARPS`，应看到 `#ifndef`/`#define`/`#endif` 三件套。
- 对比 `cpp.txt` 与 `vh.txt`：结构完全对称，只是 `#` 换成了反引号 `` ` ``。

**预期结果**：三种输出都成功生成，且对 `VX_CFG_NUM_WARPS` 这类整数键的表现符合上表。若 `cflags` 命令缺了 `-DVX_CFG_XLEN=32`，会报 `Undefined key 'VX_CFG_XLEN'`——这正是 4.4 节要解释的「枚举轴必须外部喂值」。

> 说明：以上命令在本讲撰写时未实际运行（沙箱限制），具体空格/换行细节请以本地运行为准，标记为**待本地验证**；但三种格式的结构形态由源码完全确定。

#### 4.1.5 小练习与答案

**练习 1**：`--format` 一共有几种合法取值？为什么 `cflags` 不能和 `cpp`/`verilog` 一样有 unresolved 模式？

**参考答案**：三种——`cflags`/`cpp`/`verilog`（见 [gen_config.py:L1488](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1488)）。`cflags` 是编译器命令行标志，必须是一个确定的 `-DNAME=value` 字符串，没有「守卫」这种语法可以挂，所以它**永远 resolved**；源码里也显式写了 `cflags` 不允许 unresolved（[gen_config.py:L1544-L1545](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1544-L1545)）。

**练习 2**：同一个 `VX_CFG_NUM_WARPS=4`，为什么 cflags 输出 `-DVX_CFG_NUM_WARPS=4`，而 unresolved 头要包一层 `#ifndef`？

**参考答案**：因为头文件会被多处 include，且希望编译者能用命令行 `-DVX_CFG_NUM_WARPS=8` **抢先定义**来覆盖默认值；`#ifndef` 守卫就是「若未被外部定义才给默认」。而 cflags 本身就是命令行标志，它**就是**那个「外部定义」，自然不需要再守卫自己。

---

### 4.2 值流地图：configure 生成头文件，make 生成 cflags

#### 4.2.1 概念说明

光知道生成器能产出三种格式还不够，关键是搞清楚**谁在什么时刻调用它、产物给谁**。设计文档 [build_configuration_system.md §2](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L36-L44) 给出了一张值流图，本节把它拆成两条腿：

- **第一条腿（configure 时刻）**：`configure` 脚本遍历所有 `*.toml`，对每个 toml 生成一个 `.vh`（verilog）和一个 `.h`（cpp）。这些头文件落进 `build/hw/` 与 `build/sw/`，被 RTL 和 runtime/sim 直接 include。
- **第二条腿（make 时刻）**：每个 `common.mk` 在编译 kernel/test/runtime 时，调用 `gen_config.py --format cflags`，把**已求值**的配置投影成一串 `-DVX_CFG_*` 标志，塞进 `CFLAGS`。

两条腿缺一不可：第一条腿服务于「需要 include 头文件」的代码（RTL、runtime C++），第二条腿服务于「希望用命令行 `-D` 覆盖、且不想 include 私有头文件」的代码（kernel/test，它们受 u2-l1 的边界隔离约束，不能 include `VX_config.h`）。

#### 4.2.2 核心流程

设计文档里的值流图（[build_configuration_system.md:L37-L44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L37-L44)）原文如下，本讲略作中文标注：

```
VX_config.toml ──gen_config.py──► build/hw/VX_config.vh (`define) ──► RTL (`VX_CFG_*)
                                └─► build/sw/VX_config.h  (#define) ──► sim/runtime
                gen_config.py --cflags ──► -DVX_CFG_*      ──► sim & kernel/test builds

VX_types.toml  ──gen_config.py──► build/hw/VX_types.vh     ──► RTL (`VX_MEM_*, `VX_CSR_*)
                                └─► build/sw/VX_types.h    ──► SW/runtime (ABI 契约)
```

合并两条腿后，完整的值流是：

```
            ┌─ configure 时刻（gen_header，每个 toml → .vh + .h）─────────────┐
*.toml ────►│  VX_config → VX_config.{vh,h}  (unresolved，可被 -D 覆盖)        │
            │  VX_types  → VX_types.{vh,h}   (--resolved，固定 ABI 常数)       │
            └────────────────────────┬───────────────────────────────────────┘
                                     │ include
                                     ▼
                          RTL（via VX_define.vh）/ runtime C++
            ┌─ make 时刻（common.mk，gen_config.py --format cflags）──────────┐
*.toml ────►│  VX_config + $(CONFIGS) + -DVX_CFG_XLEN=$(XLEN) → -DVX_CFG_*    │
            └────────────────────────┬───────────────────────────────────────┘
                                     │ 追加进 VX_CFLAGS
                                     ▼
                          kernel / test 的 clang 编译命令
```

注意两个细节：① `VX_config` 的头是 **unresolved**（保留覆盖能力），而 `VX_types` 的头是 **resolved**（ABI 契约必须固定）；② cflags 这条腿**只**针对 `VX_config.toml`，且**总是**带上 `-DVX_CFG_XLEN=$(XLEN)`。

#### 4.2.3 源码精读

**第一条腿：configure 驱动头文件生成。** [`configure`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L216-L237) 定义了一个 `gen_header` 函数并遍历 toml：

- [`gen_header`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L216-L224)：带增量检查——仅当 toml、`gen_config.py`、或配置签名 `.config.stamp` 比目标文件新时才重新生成，避免每次 configure 都全量重算。
- [遍历循环](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L228-L237)：对每个 toml 产出 `hw/<name>.vh` 与 `sw/<name>.h`；其中 `VX_types` 额外加 `--resolved`。
- [`export XLEN`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L227)：把 `XLEN` 导出为环境变量，供 `VX_types.toml` 里的 `[[builtin]] XLEN` 读取（见 4.4 节）。

**第二条腿：make 驱动 cflags。** 以 [`tests/regression/common.mk:L14`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/common.mk#L14) 为例（runtime/opencl/mpi/graphics 等所有 `common.mk` 都有同一行）：

```makefile
XCONFIGS := $(shell python3 $(ROOT_DIR)/ci/gen_config.py \
            --config=$(VORTEX_HOME)/VX_config.toml \
            --cflags='$(CONFIGS) -DVX_CFG_XLEN=$(XLEN)')
```

它把用户传入的 `CONFIGS`（比如 `-DVX_CFG_NUM_CORES=2`）和 `-DVX_CFG_XLEN=$(XLEN)` 拼成 `--cflags`，让生成器求值后吐出一串完整的 `-DVX_CFG_*`，再由 [common.mk:L59-L61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/common.mk#L59-L61) 追加进 `VX_CFLAGS`：

```makefile
# Project the resolved hardware config to -DVX_CFG_* flags so kernel/test code
# need not #include <VX_config.h>.
VX_CFLAGS += $(XCONFIGS)
```

这句注释是整条值流的点睛之笔：**cflags 这条腿存在的意义，就是让 kernel/test 不必 include 私有的 `VX_config.h`**——这正好是 u2-l1/u2-l3 边界隔离的工程实现。

**RTL 侧的入口**：生成的两个 `.vh` 由 [`hw/rtl/VX_define.vh`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh#L17-L19) 一次性 include：

```verilog
`include "VX_platform.vh"
`include "VX_config.vh"
`include "VX_types.vh"
```

RTL 直接引用 `` `VX_CFG_* `` / `` `VX_MEM_* `` 宏，**没有** SystemVerilog package（这一点设计文档 §4 有专门解释，本讲不展开）。

#### 4.2.4 代码实践

**目标**：在真实构建产物里看到两条腿的产物。

**操作步骤**：

1. 在 `build/` 目录运行 `../configure --xlen=64`（参考 u1-l3）。
2. 打开 `build/hw/VX_config.vh` 与 `build/sw/VX_config.h`，确认它们是 unresolved（满是 `` `ifndef ``/`#ifndef`）。
3. 打开 `build/hw/VX_types.vh`，确认它是 resolved（直接 `` `define VX_MEM_USER_BASE_ADDR ... ``，没有守卫），并对照 [`VX_types.toml:L21`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L21) 的 `expr: 0x...00010000 if ($XLEN == 64) else 0x00010000`，验证 64 位分支已被求值。
4. 进 `tests/regression/demo` 跑一次 `make`，在编译命令里找到 `-DVX_CFG_NUM_WARPS=...` 这类标志（即 `$(XCONFIGS)` 的展开）。

**需要观察的现象**：

- `VX_config.vh` 里 `VX_CFG_NUM_CORES` 这类键带 `ifndef`，可被命令行覆盖；`VX_types.vh` 里 `VX_MEM_*` 是固定常数。
- `make` 的编译命令行末尾出现一长串 `-DVX_CFG_*`，其中 `VX_CFG_XLEN=64` 一定在场。

**预期结果**：两条腿的产物分别出现在 `build/{hw,sw}/` 和编译命令行里。**待本地验证**（依赖一次完整 configure + make）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VX_config` 的头是 unresolved，而 `VX_types` 的头是 resolved？

**参考答案**：`VX_config` 是可调的硬件构建配置，用户常在命令行用 `-DVX_CFG_*` 临时覆盖（如 `--cores=2`），所以必须保留 `ifndef` 守卫；`VX_types` 是软硬件共享的 ABI 契约（CSR/DCR 编号、内存映射、页表格式），这些是**固定**的架构常数，不该被随意覆盖，所以 configure 用 `--resolved` 把它们烘焙成固定值（见 [configure:L232-L234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L232-L234)）。

**练习 2**：kernel 代码需要知道 `NUM_WARPS`，但它又被禁止 include `VX_config.h`。这个矛盾怎么解决？

**参考答案**：靠 cflags 这条腿——`common.mk` 调 `gen_config.py --format cflags` 把 `VX_CFG_NUM_WARPS` 投影成 `-DVX_CFG_NUM_WARPS=4` 编译标志，kernel 代码直接用宏名即可，无需 include 任何私有头文件。

---

### 4.3 resolved vs unresolved：两种解析模式

#### 4.3.1 概念说明

同一个 toml，生成器有两套截然不同的「翻译策略」：

- **resolved（已求值）**：在**生成头文件时**就把所有 `expr:` 表达式算成确定常数，输出 `#define KEY 42` 这样的死值。优点是消费者无需再算；缺点是用户无法再用 `-D` 覆盖。
- **unresolved（未求值）**：在生成头文件时**保留表达式结构**，用 `ifndef`/`ifdef` 守卫 + 宏表达式把它原样翻译成预处理器能 later 求值的形式，输出 `#ifndef KEY` / `#define KEY ((A) / (B))` / `#endif`。优点是用户能用 `-D` 在编译时刻覆盖任意输入；缺点是输出更复杂（条件表达式会展开成嵌套 `ifdef` 树）。

理解两者的关键是认清「求值发生在哪一刻」：resolved 在 Python 里当场算完；unresolved 把求值**推迟到 C/Verilog 预处理器**，由编译时的 `-D` 决定输入。

#### 4.3.2 核心流程

**resolved 模式**由 [`Resolver`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1270-L1339) 类驱动：它对每个键递归求值，遇到 `expr:` 就用 Python `eval`（在受限作用域里）算出常数，结果写进 `cfg` 字典，再交给 `emit_cflags` / `emit_resolved_header` 输出。

**unresolved 模式**由 [`emit_unresolved_header`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1170-L1224) 驱动：它**不**求值，而是把 `expr:` 翻译成预处理器表达式。翻译时对每个 AST 节点做方言映射（`emit_cflags`/`emit_resolved_header` 不需要这步）：

```
Python AST 节点          unresolved 输出（cpp 方言）
─────────────────────────────────────────────────
$VX_CFG_NUM_WARPS        VX_CFG_NUM_WARPS         （名字原样，靠 -D 提供）
a + b                    ((a) + (b))
a / b                    ((a) / (b))
a and b                  ((a) && (b))
up(x)                    __UP(x)                  （注入辅助宏）
clog2(x)                 __CLOG2(x)               （注入辅助宏）
X if C else Y            `ifdef C / #define X / `else / #define Y / `endif
```

`unresolved` 还要额外做两件 `resolved` 不做的事：① 在文件顶部**注入辅助宏**（`__UP`/`__CLOG2`/`__MIN`/`__MAX`/`__CLAMP`/`__POW`，见 [HELPERS:L650-L657](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L650-L657)）；② 把条件表达式 `X if C else Y` 展开成嵌套 `ifdef` 树（[`_emit_test_tree`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L861-L918)）。

#### 4.3.3 源码精读

以 [`VX_CFG_ISSUE_WIDTH = "expr: up($VX_CFG_NUM_WARPS / 16)"`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L50) 为例（默认 `NUM_WARPS=4`，故 `up(4/16)=up(0)=1`），两种模式的对照：

| 模式 | 输出（由源码逻辑推导） |
| --- | --- |
| **resolved**（cflags / `--resolved`） | `#define VX_CFG_ISSUE_WIDTH 1`（cflags: `-DVX_CFG_ISSUE_WIDTH=1`） |
| **unresolved**（默认头文件） | `#ifndef VX_CFG_ISSUE_WIDTH`<br>`#define VX_CFG_ISSUE_WIDTH __UP(((VX_CFG_NUM_WARPS) / (16)))`<br>`#endif`<br>（文件顶部另注入 `__UP` 宏定义） |

unresolved 版本里的 `__UP` 由 [`UpHelper`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L570-L580) 注入：`` `define __UP(x) (((x) != 0) ? (x) : 1) ``（cpp 版本相同，只是 `#`）。

再看一个**布尔带 `_ENABLE`** 的例子 [`VX_CFG_DCACHE_ENABLE = true`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L14)。unresolved 模式会同时输出宏本身和它的 `_ENABLED` 整数镜像（[`_emit_enabled_companion_unresolved`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1156-L1167)）：

```c
#ifndef VX_CFG_DCACHE_DISABLE
#ifndef VX_CFG_DCACHE_ENABLE
#define VX_CFG_DCACHE_ENABLE
#endif
#endif

#ifndef VX_CFG_DCACHE_ENABLED
#ifdef VX_CFG_DCACHE_ENABLE
#define VX_CFG_DCACHE_ENABLED 1
#else
#define VX_CFG_DCACHE_ENABLED 0
#endif
#endif
```

关键观察：`_ENABLED` 是**从 `_ENABLE` 的「是否被定义」反推**出来的（`ifdef VX_CFG_DCACHE_ENABLE` → 1，否则 0），所以即便用户用 `-DVX_CFG_DCACHE_DISABLE` 关掉它，`_ENABLED` 也自动跟着变 0——这正是 u2-l1 强调的「两副面孔、单一真相、不漂移」在 unresolved 模式下的实现。

> resolved 模式下的 `_ENABLED` 镜像由 [`Resolver.resolve` 的自动派生分支](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1317-L1322)提供：遇到未在 TOML 里出现的 `X_ENABLED`，就回退算 `1 if X_ENABLE else 0`。

#### 4.3.4 代码实践

**目标**：亲手对同一 toml 触发两种模式，观察 `expr:` 表达式在两副面孔下的差别。

**操作步骤**：

```bash
# resolved 头（强制求值）
python3 ci/gen_config.py -c VX_config.toml -f cpp -r --cflags='-DVX_CFG_XLEN=32' > /tmp/cpp_resolved.txt
# unresolved 头（默认）
python3 ci/gen_config.py -c VX_config.toml -f cpp                          > /tmp/cpp_unresolved.txt
# diff 同一个键在两种模式下的表现
diff <(grep -A2 'VX_CFG_ISSUE_WIDTH' /tmp/cpp_unresolved.txt) \
     <(grep -A2 'VX_CFG_ISSUE_WIDTH' /tmp/cpp_resolved.txt)
```

**需要观察的现象**：

- unresolved 版：`VX_CFG_ISSUE_WIDTH` 是 `__UP(((VX_CFG_NUM_WARPS) / (16)))`，文件顶部有 `__UP` 宏；`VX_CFG_DCACHE_ENABLE` 带 `ifndef` 守卫 + `_ENABLED` 镜像块。
- resolved 版：`VX_CFG_ISSUE_WIDTH` 直接是 `1`；`VX_CFG_DCACHE_ENABLE` 是 `#define VX_CFG_DCACHE_ENABLE` + `#define VX_CFG_DCACHE_ENABLED 1`，无任何守卫。

**预期结果**：diff 清楚显示「同一表达式，一个是宏公式、一个是死常数」。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：在 unresolved 头里，为什么 `VX_CFG_DCACHE_ENABLED` 要用 `#ifdef VX_CFG_DCACHE_ENABLE` 反推，而不是直接写死 `1`？

**参考答案**：因为 unresolved 头的整个目的就是「允许编译时刻覆盖」。用户可能用 `-DVX_CFG_DCACHE_DISABLE` 把 dcache 关掉；如果 `_ENABLED` 写死成 1，就和 `_ENABLE` 漂移了。用 `ifdef` 反推，保证 `_ENABLED` 永远忠实反映 `_ENABLE` 当下的开关状态（见 [`_emit_enabled_companion_unresolved`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1156-L1167)）。

**练习 2**：把 `--resolved` 加到 `VX_config.toml` 的头文件生成上（即让 configure 对 VX_config 也用 resolved），会破坏什么？

**参考答案**：会破坏「命令行覆盖」能力。resolved 头是死常数，`--cores=2` 这类 `-DVX_CFG_NUM_CORES=2` 就无法再改变 RTL/runtime 看到的值，因为头文件已经把它烘焙成默认值了。所以 configure 故意只对 `VX_types` 用 `--resolved`，保留 `VX_config` 的 unresolved（[configure:L232-L234](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/configure#L232-L234)）。

---

### 4.4 expr: 表达式语言与符号表

#### 4.4.1 概念说明

光有字面量远远不够——硬件配置充满派生关系（「issue 宽度 = 上取整(warp 数/16)」「L2 line = 2×block，若 L2 开启」）。`gen_config.py` 用一套**小型表达式语言**表达这些派生：

- **语法**：值写成 `expr: <Python 表达式>`（或反引号包裹 `` `...` ``），表达式里用 `$NAME` 或 `${NAME}` 引用其他键。
- **支持的运算**：四则运算（`/` 是整数除法）、位运算、比较、`and/or/not`、三元 `X if C else Y`。
- **内置函数**：`up`（上取整到非零）、`clog2`、`min/max/clamp/pow`、`int/bool`。
- **四类符号**：普通配置键、`[[enum]]` 枚举、`[[builtin]]` 环境变量、`[[param]]` RTL 提供 localparam，以及小写私有局部量。

其中后三类是初学者最容易困惑的，本节逐一拆解。

#### 4.4.2 核心流程

表达式求值（resolved 模式）的核心是 [`Resolver`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1270-L1361)。它按下面的优先级查一个符号的值：

```
Resolver.resolve(key):
  1. 命中缓存？            → 直接返回
  2. key 在 [[builtin]]？  → 从环境变量取值（os.environ），按声明类型转换
  3. key 在 [[param]]？    → 返回类型默认值（0；实际值由 RTL localparam 提供）
  4. key 在 overrides？    → 返回 -D 覆盖值
  5. key 是「ENUM_SUFFIX」？→ 取枚举当前值，与 suffix 比较，返回布尔
  6. key 是「X_ENABLED」？  → 自动派生 1 if X_ENABLE else 0
  7. key 在 TOML 表里？    → 取其值（字面量 或 递归 eval 其 expr:）
  8. 都不是                → 报错 Undefined key
```

两个易错点：① `$NAME` 在送进 `eval` 前会被 `_preprocess_expr` 剥掉 `$`（[gen_config.py:L134-L137](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L134-L137)），所以表达式其实用裸名字查找；② `/` 被 [`_IntDivXform`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1235-L1248) 改写成整数除法，**必须**如此，否则 `up(4/16)` 会算成 `up(0.25)=0.25` 而非 `up(0)=1`，导致 cflags 与头文件分叉。

#### 4.4.3 源码精读

**① `[[builtin]]` —— 来自环境变量的构建轴。** 最典型的就是 `XLEN`。它在 [`VX_types.toml:L839-L840`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L839-L840) 声明：

```toml
[[builtin]]
XLEN = "int"
```

于是 [`VX_types.toml:L21`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L21) 的 `expr: 0x...00010000 if ($XLEN == 64) else 0x00010000` 就能读到 `configure` 用 `export XLEN` 注入的值。这正是 4.2 节里 `configure` 必须 `export XLEN` 的原因。Resolver 的取值逻辑在 [gen_config.py:L1287-L1295](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1287-L1295)：未设置时回退到类型默认值（int→0，即 32 位）。

注意：`[[builtin]]` 变量**不会被输出**到任何格式（[gen_config.py:L30 注释](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L30)），它只在表达式求值时存在。

**② `[[enum]]` —— 受限取值的枚举轴。** [`VX_config.toml:L381-L385`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L381-L385) 声明了 `VX_CFG_XLEN=[32,64]` 等枚举。表达式里常用「枚举后缀」写法，如 [`VX_CFG_EXT_D_ENABLE = "expr: $VX_CFG_XLEN_64"`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L24)：这里的 `VX_CFG_XLEN_64` 不是新键，而是「`VX_CFG_XLEN` 是否等于 64」的布尔（[Resolver:L1306-L1312](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1306-L1312)）。

> **关键陷阱**：`VX_CFG_XLEN` 只在 `[[enum]]` 里声明了允许值 `[32,64]`，**没有**在任何一个 TOML 表里给它默认值。所以它必须由外部 `-DVX_CFG_XLEN=$(XLEN)` 喂值；否则 resolved 求值会走到第 8 步报 `Undefined key 'VX_CFG_XLEN'`。这就是为什么所有 `common.mk` 的 cflags 调用都硬带 `-DVX_CFG_XLEN=$(XLEN)`，也是本讲 4.1 实践里 cflags 命令必须加这个 `-D` 的根本原因。

**③ `[[param]]` —— RTL 提供的 localparam。** [`VX_config.toml:L387-L390`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L387-L390) 声明 `VX_CFG_DCACHE_NUM_REQS = "int"` 等。这些是「方向反过来」的符号：**值不是 TOML 给的，而是 RTL 在综合时用 localparam 提供的**。所以在 resolved 模式里它们取类型默认值 0（仅占位，让表达式能算通），而在 unresolved 头里它们被翻译成**不带 `VX_CFG_` 前缀的短名**供 SV localparam 接管（见 [`_name_ref` 的 param 分支](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L759-L765)）。`[[param]]`/`[[builtin]]` 都是只读符号，若试图用 `-D` 覆盖或当普通键赋值，[main() 会显式报错](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1508-L1524)。

**④ 小写私有变量 —— 只在 TOML 内部存在的中间量。** 形如 [`dpi_is_enabled = "expr: $SV_DPI"`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L11) 或 [`fpu_dsp_quartus`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L112)。判定规则极简：**名字全大写才是公开配置键（`_has_public_scope`，[L130-L131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L130-L131)）；含小写字母的就是私有局部量，永不输出**。它们只在被公开键的 `expr:` 引用时，被 [`_inline_locals_into`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L730-L740) 内联展开。比如 [`VX_CFG_FPU_TYPE`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L103) 引用了 `$dpi_is_enabled`，unresolved 输出时 `$dpi_is_enabled` 会被替换成 `SV_DPI`（局部量的表达式被就地代入）。这让你能把复杂派生拆成有名字的小步骤，又不污染对外宏命名空间（设计文档 §3.2 把这类与 `VX_CFG_*` 严格区分）。

#### 4.4.4 代码实践

**目标**：体会四类符号在 resolved 求值中的不同来源。

**操作步骤**：

```bash
# A. 用环境变量喂 [[builtin]]，观察 VX_types 的 SV39/SV32 切换
XLEN=64 python3 ci/gen_config.py -c VX_types.toml -f cpp -r | grep 'VX_VM_ADDR_MODE\|VX_MEM_USER_BASE_ADDR'
XLEN=32 python3 ci/gen_config.py -c VX_types.toml -f cpp -r | grep 'VX_VM_ADDR_MODE\|VX_MEM_USER_BASE_ADDR'

# B. 用 -D 喂 [[enum]]，观察 VX_config 里 EXT_D 的连锁派生
python3 ci/gen_config.py -c VX_config.toml -f cpp -r --cflags='-DVX_CFG_XLEN=32' | grep 'VX_CFG_EXT_D_ENABLE\|VX_CFG_FLEN '
python3 ci/gen_config.py -c VX_config.toml -f cpp -r --cflags='-DVX_CFG_XLEN=64' | grep 'VX_CFG_EXT_D_ENABLE\|VX_CFG_FLEN '
```

**需要观察的现象**：

- A：`XLEN=64` 时 `VX_VM_ADDR_MODE` 求值为 `SV39`、`VX_MEM_USER_BASE_ADDR` 取 64 位分支地址；`XLEN=32` 时变为 `SV32` 与 32 位地址。这证明 `[[builtin]] XLEN` 真的来自环境变量。
- B：`XLEN=32` 时 `VX_CFG_EXT_D_ENABLE` 为关、`VX_CFG_FLEN=32`；`XLEN=64` 时 `VX_CFG_EXT_D_ENABLE` 开、`VX_CFG_FLEN=64`。这演示了「枚举后缀 `$VX_CFG_XLEN_64` → EXT_D → FLEN」的连锁派生（对照 [VX_config.toml:L24-L25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L24-L25)）。

**预期结果**：两组 grep 输出随 `XLEN` 不同而不同。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `gen_config.py` 在求值表达式时必须把 `/` 改写成整数除法（`_IntDivXform`）？

**参考答案**：因为生成的 C/Verilog 把 `/` 当整数（截断）除法，Python 默认的 `/` 却是浮点除法。若不改写，`up(4/16)` 在 Python 里是 `up(0.25)=0.25`，而头文件/cflags 输出的是 `up(0)=1`，两边会分叉。设计文档注释里专门举了这个例子（[gen_config.py:L1238-L1242](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L1238-L1242)）。

**练习 2**：`dpi_is_enabled`（小写）和 `VX_CFG_DCACHE_ENABLE`（大写）在生成器眼里有什么本质区别？

**参考答案**：`_has_public_scope` 判定——全大写才是公开配置键，会被输出为宏；含小写字母的是私有局部量，**永不输出**，只在被公开键引用时内联展开（[L130-L131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/gen_config.py#L130-L131) + 设计文档 §3.2）。这让 `VX_CFG_*` 保持「真实可配置项的诚实清单」，而不是掺杂一堆中间计算量。

**练习 3**：在 `VX_types.toml` 里 `XLEN` 是 `[[builtin]]`，在 `VX_config.toml` 里却是 `[[enum]] VX_CFG_XLEN`。为什么同一个轴有两种身份？

**参考答案**：两个 toml 是**分两次、不同变量作用域**生成的（见 [VX_types.toml:L836-L838 注释](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L836-L838)）。`VX_types` 里它以裸 `XLEN` 从环境变量来；`VX_config` 里它以 `VX_CFG_XLEN` 枚举形式存在、由 `-DVX_CFG_XLEN=$(XLEN)` 喂值。两者必须 lockstep，但身份不同。

## 5. 综合实践

把本讲四节串起来，完成一次「**追踪一个旋钮从命令行到 RTL 宏的全旅程**」：

1. **选一个旋钮**：以 `--cores=2`（即 `NUM_CORES=2`）为例。
2. **第一条腿验证**：在 `build/` 跑 `../configure --xlen=32`，打开 `build/hw/VX_config.vh`，找到 `VX_CFG_NUM_CORES`——确认它是 unresolved（带 `` `ifndef ``），所以仍可被覆盖。
3. **第二条腿验证**：进 `tests/regression/demo`，用 `make CONFIGS='-DVX_CFG_NUM_CORES=2'`（或参照 u1-l4 用 blackbox）编译，**在编译命令里**找到 `-DVX_CFG_NUM_CORES=2`，并确认它来自 `$(XCONFIGS)`（即 `common.mk` 对 `gen_config.py --format cflags` 的调用）。
4. **派生链验证**：`NUM_CORES` 会影响哪些派生量？对照 `VX_config.toml` 找出引用 `$VX_CFG_NUM_CORES` 的 `expr:`（如 [`VX_CFG_AMO_RS_SIZE`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L130)、[`VX_CFG_NUM_DXA_CORES`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L260) 等），手动用 cflags 生成器求值 `--cflags='-DVX_CFG_XLEN=32 -DVX_CFG_NUM_CORES=2'`，grep 出这些派生键，核对它们的值确实随 `NUM_CORES=2` 变化。
5. **画一张值流图**：把上述旅程画成「`--cores=2` → `CONFIGS` → `gen_config.py --cflags` → `-DVX_CFG_*` → `VX_CFLAGS` → clang → kernel；同时 `VX_config.toml` → `gen_config.py` → `VX_config.vh` → RTL」的双腿图。

**交付物**：一张标注了「旋钮值在每一跳的形态」的值流图，以及对至少 2 个派生键的求值核对记录。整个任务**待本地验证**（需一次完整 configure + make）。

## 6. 本讲小结

- `gen_config.py` 是一台「TOML → 宏定义」翻译机，输出 **cflags / cpp / verilog** 三种格式：cflags 给 sim/kernel/test 的命令行，cpp 头给 runtime/sim，verilog 头给 RTL。同一个值只换语法外壳。
- 值流有**两条腿**：configure 时刻生成 `.h`/`.vh` 头文件（`VX_config` unresolved、`VX_types` resolved），make 时刻由各 `common.mk` 调 `--format cflags` 投影出 `-DVX_CFG_*` 标志——后者让 kernel/test 不必 include 私有 `VX_config.h`。
- 两种解析模式：**resolved** 在 Python 里当场求值成死常数；**unresolved** 把求值推迟到 C/Verilog 预处理器，用 `ifndef`/`ifdef` 守卫保留 `-D` 覆盖能力。`cflags` 永远 resolved。
- `expr:` 表达式语言用 `$NAME` 引用其他键，支持四则/位运算/三元/`up`/`clog2` 等；`/` 被强制改写为整数除法以防 cflags 与头文件分叉。
- 四类符号各有来源：`[[builtin]]` 来自环境变量（如 `XLEN`）、`[[enum]]` 是受限取值轴（如 `VX_CFG_XLEN=[32,64]`，常用 `$KEY_VALUE` 后缀语法）、`[[param]]` 由 RTL localparam 提供、小写名为 TOML 内部私有量不输出。
- `VX_CFG_XLEN` 只在 `[[enum]]` 声明、无默认值，所以 cflags 调用**必须**带 `-DVX_CFG_XLEN=$(XLEN)`，否则求值报 `Undefined key`——这是理解整条值流的一个关键陷阱。

## 7. 下一步学习建议

- **下一讲 u2-l3（硬件/软件分层与边界检查）**：本讲多次提到「kernel/test 不能 include `VX_config.h`」「`VX_types` 是 ABI 契约」。u2-l3 会用 `check_config_boundary.sh` / `check_sw_sim_boundary.sh` 两个 CI 脚本，讲清楚这条 HW↔SW 边界**如何被强制**。
- **回看 u1-l3**：本讲的「configure 是模板填空机 + 调 gen_config.py」是对 u1-l3 第 4 节的深化；若对 `sed @占位符@` 与 `.config.stamp` 增量机制生疏，建议重读。
- **延伸阅读**：[`docs/designs/build_configuration_system.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md) §3（`VX_CFG_*` 命名空间）、§4（为何放弃 SystemVerilog typed-config package）——后者解释了为什么 RTL 只能用反引号宏而非 package。
- **动手验证**：把本讲所有标了「待本地验证」的命令在本地 `build/` 树里跑一遍，是巩固值流图最快的方式。
