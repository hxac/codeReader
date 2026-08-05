# 硬件/软件分层与边界检查

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `VX_config`（HW/sim 私有构建配置）与 `VX_types`（软硬件共享 ABI 契约）这条分层的来龙去脉，以及为什么公共运行时头文件**绝不能** `#include "VX_config.h"`。
- 读懂两个 CI 边界守卫脚本 `ci/check_config_boundary.sh` 与 `ci/check_sw_sim_boundary.sh` 的判定规则，知道它们各自拦什么、用什么正则、在哪里被调用。
- 动手触发一次违规，亲眼看到脚本如何报错，从而理解「规则是机械执行的，不是靠自觉」。
- 掌握 `sw/common/` 这条合法的「跨层逃生通道」何时该用。

本讲承接 u2-l1（TOML 是唯一真相来源）与 u2-l2（`gen_config.py` 的值流），回答一个工程问题：**配置值已经在两套 TOML 里分好了，仓库靠什么机制保证没人把私有配置漏进软件层？**

## 2. 前置知识

- **ABI（Application Binary Interface）**：二进制层面的契约。两段代码（哪怕用不同语言、不同编译选项编译）只要遵守同一份 ABI，就能正确交换数据。Vortex 里 RTL 和软件运行时是两套独立编译产物，它们之间的「设备内存布局」「CSR/DCR 编号」「页表格式」就是 ABI。
- **私有 vs 共享**：`VX_config.toml` 描述的是某一**次具体构建**的微架构参数（缓存多大、流水线多宽）——这是实现细节，下游软件不该看见；`VX_types.toml` 描述的是 ISA/ABI 契约——任何软件只要对接 Vortex 就必须遵守，必须共享。
- **`#include` 与 `-I` 的关系**：`#include "foo.h"` 能否找到文件，取决于编译命令里 `-I` 给出的搜索路径。所以「层间隔离」既要在源码层拦 `#include`，也要在构建层拦 `-I` 路径，两条路都得堵。
- **`grep -rnE`**：递归（`-r`）、带行号（`-n`）、扩展正则（`-E`）。两个守卫脚本本质上就是几条精心设计的 `grep`。

> 本讲用到的配置生成与值流知识来自 u2-l2，不再重复；只聚焦「值流到了之后，如何被守住边界」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ci/check_config_boundary.sh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_config_boundary.sh) | 守卫 ①：禁止 `sw/` 与 `tests/` 里出现 `#include "VX_config.h"`。 |
| [ci/check_sw_sim_boundary.sh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_sw_sim_boundary.sh) | 守卫 ②：强制 `sw/{kernel,runtime}` 与 `sim/`+`hw/` 双向隔离（`#include` 与 `-I` 双管齐下）。 |
| [ci/regression.sh.in](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in) | 回归脚本，在开头依次调用上面两个守卫；`set -e` 下任一失败即中止。 |
| [hw/rtl/VX_define.vh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh) | RTL 的全局包含头，一次性 `include` 两个生成头 `VX_config.vh` 与 `VX_types.vh`——展示了「RTL 可以同时吃两份，软件不行」。 |
| [sw/runtime/include/vortex2.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h) | 公共运行时头，刻意「自包含」，注释明确声明永不 include 构建配置，改用 `vx_device_query()` 在运行时查询。 |
| [docs/designs/build_configuration_system.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md) | 设计文档 §2，用一张值流图把分层讲透。 |
| [docs/coding_guidelines_cpp.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_cpp.md) | 编码规范 §8，定义双向隔离规则与 `sw/common/` 逃生通道。 |

## 4. 核心概念与源码讲解

### 4.1 分层模型：私有构建配置 vs 共享 ABI 契约

#### 4.1.1 概念说明

u2-l1 已经确立：所有配置常量集中在两个 TOML 里。但「集中」不等于「人人都能看」。Vortex 把这两个 TOML 的产物划成两个**可见性等级**：

- **`VX_config`（私有）**：来自 `VX_config.toml`，约 245 个键，全是「这次硬件具体怎么搭」的微架构参数（缓存容量、缓冲深度、流水线宽度、各加速器开关）。它的生成头 `VX_config.h` / `VX_config.vh` 只许 RTL（`hw/`）和仿真器（`sim/`）使用。
- **`VX_types`（共享）**：来自 `VX_types.toml`，是 ISA/ABI 契约（`VX_CSR_*`/`VX_DCR_*`/`VX_ISA_*` 编号，以及从 config 搬过来的 `[memmap]`/`[vm]` 段）。RTL 吃 `.vh` 版，软件吃 `.h` 版——同一份内容，两种语法外壳。

为什么要把 `[memmap]`（设备内存映射）和 `[vm]`（页表格式）**特意从 `VX_config.toml` 搬到 `VX_types.toml`**？因为这两样是 RTL 和软件**都必须遵守**的真正契约。留在 config 里它们就会被判为「私有」，软件就碰不到；搬到 types 里，软件就能通过共享的 `VX_types.h` 合法读到，而无需去碰私有的 `VX_config.h`。这是一次有意识的「按可见性归档」。

一句话：**软件该知道的放 types，软件不该知道的留 config。**

#### 4.1.2 核心流程

设计文档 §2 用一张值流图把分层讲死了（下一节 4.1.3 会引用原文）。用文字概括成两条腿：

```
VX_config.toml ──gen_config.py──► VX_config.vh ──► RTL        ┐
                              └─► VX_config.h  ──► sim/runtime ┘ 私有：软件/test 禁触
                --cflags ──► -DVX_CFG_* ──► sim & kernel/test 构建（以编译标志注入，不 include 头）

VX_types.toml  ──gen_config.py──► VX_types.vh ──► RTL          ┐
                              └─► VX_types.h  ──► SW/runtime   ┘ 共享 ABI 契约
```

关键区别在第 1 行与第 4 行：`VX_config` 的 `.h` 只能被 `sim/runtime` **内部**消费（runtime 的实现 `.cpp` 可以用，但 runtime 的**公共头**不行），而 `VX_types` 的 `.h` 是任何软件都能 include 的共享契约。

> 注意 `--cflags` 那条腿（u2-l2 讲过）：kernel/test 拿配置值的方式是编译命令里的 `-DVX_CFG_*` 标志，**而不是** `#include "VX_config.h"`。这正是让它们「能用上配置值又不碰私有头」的工程手法。

#### 4.1.3 源码精读

**① 设计文档的分层定义**——这是整条规则的权威出处：

[docs/designs/build_configuration_system.md:51-59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/build_configuration_system.md#L51-L59) 明确写道：`VX_config.{h,vh}` 是 HW/sim 私有；真正的 HW↔SW 契约（内存映射、页表格式）被**搬迁**进 `VX_types.toml`，因此「公共运行时头永不 include `VX_config.h`」，并由两个 CI 守卫强制。

**② RTL 端：两个头都吃**。`VX_define.vh` 是 RTL 的全局包含头，开篇三行就同时 include 了私有与共享两份生成头：

```verilog
`include "VX_platform.vh"
`include "VX_config.vh"   // 私有构建配置 —— RTL 可用
`include "VX_types.vh"    // 共享 ABI 契约 —— RTL 也用
```

见 [hw/rtl/VX_define.vh:17-19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh#L17-L19)。RTL 处于「私有层」，自然两份都能看。

**③ 软件端：公共头刻意自包含**。公共运行时头 `vortex2.h` 顶部有一段注释，把「为什么不 include 配置」的纪律写在了代码里：

```c
// This public header is deliberately self-contained: it includes
// standard C headers ONLY — never VX_config.h or any other Vortex
// build-time header. Hardware configuration is discovered at runtime
// via vx_device_query(); nothing here depends on the build config.
```

见 [sw/runtime/include/vortex2.h:33-36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h#L33-L36)。它把整份文件只 include 了 `<stdint.h>`/`<stddef.h>`/`<stdio.h>` 三个标准头（见 [vortex2.h:29-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h#L29-L31)）。

**④ 那软件需要硬件参数时怎么办？**——运行时查询，而非编译时 include。`vortex2.h` 定义了一组 `VX_CAPS_*` 能力 ID，由 `vx_device_query()` 在运行时返回：

```c
#define VX_CAPS_NUM_THREADS         0x1   // 每个 warp 的线程数
#define VX_CAPS_NUM_WARPS           0x2   // 每个核的 warp 数
#define VX_CAPS_NUM_CORES           0x3   // 总核数
#define VX_CAPS_GLOBAL_MEM_SIZE     0x5   // 全局显存字节数
...
```

见 [sw/runtime/include/vortex2.h:59-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/vortex2.h#L59-L76)。这样主机程序在运行时才问设备「你多大」，而不是在编译时把 `VX_CFG_NUM_CORES` 焊死进去——既守住了边界，又支持了「同一份软件跑在不同配置的设备上」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「公共头自包含」与「软件拿配置的四条合法通道」。

**操作步骤**：

1. 打开 `sw/runtime/include/vortex2.h`，确认它只 include 了三个标准头，没有任何 `VX_config` 或 `VX_types` 的 `#include`。
2. 阅读本讲 4.2.3 引用的脚本头注释（[check_config_boundary.sh:8-12](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_config_boundary.sh#L8-L12)），它列出了软件获取配置的**四条合法通道**。
3. 在仓库里各找一处真实用例：
   - ISA/ABI 契约 → 搜 `VX_types.h` 的 include（可在 `sw/runtime/common/` 等内部实现里找到）。
   - 设备属性 → 搜 `vx_device_query`（u1-l4 的 demo 主机程序就用它查 `NUM_CORES` 等）。
   - 编译期配置 → 搜 kernel/test 的 Makefile 里 `-DVX_CFG_` 标志（u2-l2 讲过的 cflags 腿）。

**需要观察的现象**：公共头 `vortex2.h` 干干净净、与构建配置零耦合；而真正用到配置值的代码，要么在「内部实现」里（可 include），要么走「运行时查询」或「编译标志」。

**预期结果**：你能口述出「软件拿配置的四条合法通道」，并解释为什么 `vortex2.h` 一条都不用 `#include`。

#### 4.1.5 小练习与答案

**练习 1**：为什么设备内存映射（`VX_MEM_*`）被放在 `VX_types.toml` 而不是 `VX_config.toml`？

> **答案**：内存映射是 RTL 与软件**双方都要遵守**的 ABI 契约。若留在 `VX_config.toml`，它会被判为 HW/sim 私有，软件就无法合法 include；搬到 `VX_types.toml` 后，软件通过共享的 `VX_types.h` 即可读到，无需碰私有的 `VX_config.h`。

**练习 2**：`sw/runtime/common/vm.cpp`（runtime 的**实现**文件）可以 include `VX_config.h` 吗？`sw/runtime/include/vortex2.h`（runtime 的**公共头**）呢？

> **答案**：两个都不行——守卫脚本扫描的是整个 `sw/` 目录（见 4.2），不区分实现还是公共头。runtime 实现若需要配置值，应走 `VX_types.h`、`config.mk` 或 `-DVX_CFG_*` 标志，而非 include 私有头。

---

### 4.2 守卫 ①：check_config_boundary.sh（禁止软件 include VX_config.h）

#### 4.2.1 概念说明

这是「配置泄漏」守卫。它只盯一件事：**有没有哪个软件或测试文件 `#include` 了 `VX_config.h`**。它的哲学写在脚本头注释里——软件要配置值，有四条**合法**通道可走，唯独不能直接 include 私有头。

#### 4.2.2 核心流程

```
在 sw/ 与 tests/ 下递归 grep：
   正则： #include 后跟 <" 或 " 包起来的 VX_config.h
   命中 → 打印违规文件:行，退出码 1
   未命中 → 打印 "check_config_boundary: OK ..."，退出码 0
```

它由 `ci/regression.sh.in` 在回归最开头调用，`set -e` 下失败即中止整个回归。

#### 4.2.3 源码精读

**① 头注释列出四条合法通道**——这是理解「禁令替代方案」的关键：

```bash
# Software obtains what it needs from the right place instead:
#   - the ISA/ABI contract        -> VX_types.h
#   - device properties           -> vx_device_query() (VX_CAPS_*)
#   - build parameters            -> config.mk
#   - compile-time HW config      -> gen_config.py --cflags (build -D flags)
```

见 [ci/check_config_boundary.sh:8-12](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_config_boundary.sh#L8-L12)。这四条正好对应 4.1 讲的分层：契约走 types、属性走运行时查询、构建参数走 `config.mk`、编译期配置走 `-D` 标志。

**② 核心 grep**——整个守卫就这一条正则：

```bash
hits=$(grep -rnE '#[[:space:]]*include[[:space:]]*[<"]VX_config\.h[>"]' \
         "$ROOT/sw" "$ROOT/tests" \
         --include='*.c' --include='*.cpp' --include='*.cc' \
         --include='*.h' --include='*.hpp' --include='*.S' \
         2>/dev/null || true)
```

见 [ci/check_config_boundary.sh:21-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_config_boundary.sh#L21-L25)。要点：

- 扫描范围只有 `$ROOT/sw` 与 `$ROOT/tests`——**不扫** `hw/`、`sim/`（它们是合法消费者）。
- 正则里的 `VX_config\.h` 只匹配 **C 头 `.h`**，不匹配 Verilog 的 `.vh`；而且要求被 `< >` 或 `" "` 包起来，覆盖 `#include <VX_config.h>` 与 `#include "VX_config.h"` 两种写法，还能容忍 `include` 前后的空白。
- `--include` 限定只看源码扩展名，跳过文档和生成物。
- 末尾 `|| true` 是因为 `grep` 无匹配时返回非零，会让 `set -e` 误判失败；这里用「空字符串=合法」的语义绕开。

**③ 命中后的报错**——见 [check_config_boundary.sh:27-38](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_config_boundary.sh#L27-L38)：打印所有违规行，复述四条替代通道，`exit 1`。

#### 4.2.4 代码实践

**实践目标**：在干净状态下亲手跑通这个守卫，确认它「无依赖、可直接运行」。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   ./ci/check_config_boundary.sh
   ```
2. 观察输出。

**需要观察的现象**：脚本不需要先 `configure`、不需要任何构建产物——它只是 `grep`，所以在干净 checkout 上也能直接跑。

**预期结果**：打印 `check_config_boundary: OK — no sw/ or tests/ file includes VX_config.h`，退出码 0。

> 待本地验证：若你的工作树里恰好有人违规 include 了，会看到 `ERROR: ...` 与违规文件列表，退出码 1。

#### 4.2.5 小练习与答案

**练习 1**：为什么正则是 `VX_config\.h` 而不是 `VX_config`？如果有人写 `#include "VX_config.vh"`（Verilog 头）会被这个脚本拦下吗？

> **答案**：因为本守卫只防「C 头泄漏进软件」。`.vh` 是 Verilog 头，软件（C/C++）本就不会 include 它；而且 `.vh` 的泄漏属于**结构性跨层**，由守卫 ②（4.3）的 `hw/`/`sim/` 路径正则负责拦截。两者分工不同。

**练习 2**：脚本里 `2>/dev/null || true` 的 `|| true` 能去掉吗？

> **答案**：不能。脚本顶部有 `set -euo pipefail`，而 `grep` 在「没有任何匹配」时返回退出码 1。没有 `|| true` 的话，一个干净（无违规）的仓库会让这行 `grep` 以非零退出，触发 `set -e` 直接中止脚本，误报失败。`|| true` 把「无匹配」明确映射成「合法」。

---

### 4.3 守卫 ②：check_sw_sim_boundary.sh（sw ↔ sim/hw 双向隔离）

#### 4.3.1 概念说明

这是「结构性跨层」守卫，比守卫 ① 更宽。它的规则是**双向**的：

- `sw/kernel/` 与 `sw/runtime/` **不得** include 或引用 `hw/*`、`sim/*` 里的任何东西（否则把硬件/仿真内部细节漏进了面向下游的 SDK）。
- `sim/*` 与 `hw/*` **同样不得** include 或引用 `sw/kernel/`、`sw/runtime/`（否则把仿真器/RTL 耦合到了「安装面」）。

唯一合法的跨层共享通道是 `sw/common/`——一个 vortex 内部、永不安装、四个层都能访问的共享层。需要放「主机写、硬件读」的在线 ABI 结构体、主机侧硬件镜像模型、跨层公共助手时，都放这里。

#### 4.3.2 核心流程

脚本分两大段，每段都做「正向 + 反向」两次扫描：

```
第 1 段 #include 扫描
  ├─ 正向：在 sw/kernel + sw/runtime 里 grep include "…(hw|sim)/…"  → 命中即违规
  └─ 反向：在 sim + hw 里 grep include "<sw公共头或 sw/(kernel|runtime)/…>"  → 命中即违规
            （sw公共头名单 = ls sw/kernel/include/*.h 与 sw/runtime/include/*.h）

第 2 段 构建标志扫描（堵 -I 这条路）
  ├─ sim/hw 的 Makefile 不得出现 -I…/sw/(kernel|runtime)/include
  └─ sw/kernel 与 sw/runtime 的 Makefile 不得出现 -I…/(hw|sim)/
      （例外：sw/runtime/opae 排除——它必须链接 hw/syn/altera/opae/ 的 FPGA AFU 外壳）

任一段命中 → fail=1；最后 fail≠0 则打印总结并 exit 1
```

两层都扫，是因为「源码里不写 include」还不够——只要构建命令偷偷加一条 `-I…/hw/`，`#include "VX_define.vh"` 照样能编过。必须把 `#include` 与 `-I` 两条路同时堵死。

#### 4.3.3 源码精读

**① 头注释定义双向规则与逃生通道**——见 [ci/check_sw_sim_boundary.sh:1-18](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_sw_sim_boundary.sh#L1-L18)：明确「隔离是双向的」，并点名 `sw/common/` 是「vortex-internal，永不安装，四个层都能访问」的逃生通道。

**② 第 1 段：`#include` 双向扫描**。核心是一个可复用的 `scan_includes` 函数加两次调用：

```bash
# 正向：sw/kernel + sw/runtime 不得 include 来自 hw/ 或 sim/ 的头
scan_includes \
    "sw/kernel or sw/runtime references hw/ or sim/ headers:" \
    '#[[:space:]]*include[[:space:]]*[<"]([./]*(hw|sim)/[^">]+)[>"]' \
    "$ROOT/sw/kernel" "$ROOT/sw/runtime"

# 反向：sim + hw 不得 include sw 公共头
SW_PUBLIC_HEADERS=$(... ls sw/kernel/include/*.h 与 sw/runtime/include/*.h ...)
scan_includes \
    "sim/ or hw/ references sw/kernel/include or sw/runtime/include headers:" \
    "#[[:space:]]*include[[:space:]]*[<\"]($SW_PUBLIC_HEADERS|sw/(kernel|runtime)/[^\">]+)[>\"]" \
    "$ROOT/sim" "$ROOT/hw"
```

见 [ci/check_sw_sim_boundary.sh:46-62](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_sw_sim_boundary.sh#L46-L62)。正向正则里的 `[./]*(hw|sim)/` 会同时命中 `"../hw/rtl/..."`、`"hw/..."`、`"sim/simx/..."` 等各种相对写法；反向则先把两个 include 目录下的 `*.h` 文件名拼成一份「公共头名单」，再去 `sim/`+`hw/` 里找有没有人 include 这些名字或 `sw/(kernel|runtime)/…` 路径。

**③ 第 2 段：Makefile 的 `-I` 路径扫描**——见 [ci/check_sw_sim_boundary.sh:97-117](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_sw_sim_boundary.sh#L97-L117)：

```bash
# sim/* 和 hw/* 的 Makefile 不得 -I 进 sw/kernel 或 sw/runtime
scan_makefile_flags \
    "sim/ or hw/ Makefile adds -Isw/{kernel,runtime}/include:" \
    '-I[^[:space:]]*sw/(kernel|runtime)/include' \
    "$ROOT/sim" "$ROOT/hw"
```

而 `sw/kernel`、`sw/runtime` 的 Makefile 不得 `-I` 进 `hw/` 或 `sim/`，且**显式排除了 `sw/runtime/opae`**——注释解释：opae 后端必须链接 `hw/syn/altera/opae/` 里定义的 FPGA AFU 外壳，这是一处无法搬迁的硬件绑定集成，所以单独豁免。这种「规则有例外时，在脚本里显式标注并排除」的做法，比「悄悄放行」更健康。

**④ 在哪里被调用**——回归脚本开头，紧跟守卫 ①：

```bash
# Enforce the HW/SW config layering boundary ...
"@VORTEX_HOME@/ci/check_config_boundary.sh"
# Enforce the sw/ ↔ sim/+hw/ bidirectional isolation boundary.
"@VORTEX_HOME@/ci/check_sw_sim_boundary.sh"
```

见 [ci/regression.sh.in:49-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L49-L56)。文件名是 `regression.sh.in`（configure 模板），`@VORTEX_HOME@` 在 configure 时被替换成仓库绝对路径（u1-l3 讲过的「模板填空机」）。

**⑤ `sw/common/` 逃生通道**——编码规范用一张表说清了什么该放进共享层：

| 需求 | 放置位置 |
|------|----------|
| 在线 ABI 结构体（主机写 / 硬件读） | `sw/common/` |
| 主机侧硬件镜像模型 | `sw/common/` |
| 跨层公共助手（mem_alloc、bitmanip…） | `sw/common/` |
| 生成的构建配置 | `build/sw/VX_types.h`（同样共享） |

见 [docs/coding_guidelines_cpp.md:165-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/coding_guidelines_cpp.md#L165-L178)。注意守卫 ② 的扫描范围是 `sw/kernel`、`sw/runtime`、`sim`、`hw`——**不包含 `sw/common`**，这正是它成为合法逃生通道的原因（`ls sw/common/` 能看到 `vm_types.h`、`gfx_sw_abi.h`、`mem_alloc.h` 等跨层共享文件）。

#### 4.3.4 代码实践（动手触发一次违规）

**实践目标**：亲手制造一次 `sw/kernel` → `hw/` 的跨层 include，观察守卫 ② 如何报错。这是本讲的核心动手环节。

**操作步骤**：

1. 先确认干净状态：
   ```bash
   ./ci/check_sw_sim_boundary.sh
   ```
   预期看到 `sw/ ↔ sim/+hw/ boundary check OK`，退出码 0。

2. 在 `sw/kernel/` 下任选一个源文件（例如某个 `.c`），**临时**加一行故意违规的 include（示例代码，非项目原有代码）：
   ```c
   #include "../hw/rtl/VX_define.vh"   /* 故意违规：sw/kernel 引用 hw/ */
   ```

3. 再次运行：
   ```bash
   ./ci/check_sw_sim_boundary.sh; echo "exit=$?"
   ```

4. **务必**用 `git checkout -- <你改的文件>` 或手动删掉那一行，恢复原状。

**需要观察的现象**：脚本会先打印一段 `ERROR: sw/kernel or sw/runtime references hw/ or sim/ headers:`，下面列出违规的 `文件:行号:内容`，最后打印：
```
sw/ ↔ sim/+hw/ boundary check FAILED — see violations above.
See AGENTS.md §6 and docs/coding_guidelines_cpp.md §8.
```
退出码为 1。

**预期结果**：你加的那一行会被正向正则 `[./]*(hw|sim)/` 命中（`../hw/rtl/VX_define.vh` 里的 `../` 被 `[./]*` 吃掉，`hw/` 匹配 `(hw|sim)/`）。注意：守卫 ①（`check_config_boundary.sh`）**不会**拦这一行，因为它只盯 `VX_config.h` 这个 C 头名字，不盯 `.vh`，也不盯 `hw/` 路径——这正说明两个守卫分工互补。

> 待本地验证：若你给 `sim/` 下某个文件加 `#include "vx_intrinsics.h"`（来自 `sw/kernel/include/`），则会触发反向扫描的报错 `sim/ or hw/ references sw/kernel/include ...`。

#### 4.3.5 小练习与答案

**练习 1**：为什么守卫 ② 既要扫 `#include`，又要扫 Makefile 的 `-I`？只扫前者够不够？

> **答案**：不够。`#include "foo.h"` 能否找到，取决于编译命令的 `-I` 搜索路径。只要某 Makefile 偷偷加一条 `-I…/hw/rtl`，源码里哪怕只写 `#include "VX_define.vh"`（不含 `hw/` 字样）也能编过。只有同时禁止「跨层 include 文本」与「跨层 `-I` 路径」，才能把隔离真正堵死。

**练习 2**：`sw/runtime/opae` 为什么被豁免？这种豁免在脚本里是如何实现的？

> **答案**：opae 后端必须链接 `hw/syn/altera/opae/` 定义的 FPGA AFU 外壳，这是一处无法搬迁的硬件绑定集成。脚本在第 2 段扫描时用 `-not -path '*/sw/runtime/opae/*'` 把该目录排除在「sw 侧 Makefile 不得 `-I` 进 hw/sim」的检查之外（见 [check_sw_sim_boundary.sh:113-117](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/check_sw_sim_boundary.sh#L113-L117)）。规则有例外时显式标注，胜过悄悄放行。

**练习 3**：四层（`sw/kernel`、`sw/runtime`、`sim`、`hw`）想共享一份「主机写、硬件读」的结构体，应该放哪？为什么守卫不会拦？

> **答案**：放 `sw/common/`。因为两个守卫的扫描范围都只覆盖 `sw/kernel`、`sw/runtime`、`sim`、`hw`，**不包含 `sw/common`**；编码规范 §8 也明确 `sw/common/` 是「四个层都能访问、永不安装」的内部共享层。

## 5. 综合实践

把本讲的三块知识串起来，做一次「新增一份跨层共享数据」的设计演练。

**背景**：假设你要给 Vortex 加一个「主机填表、硬件读表」的命令描述符结构体（例如一个新的 DMA 描述符），它需要同时被主机运行时（`sw/runtime`）和仿真器（`sim`）/RTL（`hw`）看到。

**任务**：

1. **选址**：这个结构体该放哪个目录？分别考虑放进 `sw/runtime/include/`、`hw/rtl/`、`sw/common/` 三种选择，用本讲的规则判断哪些会被守卫拦下，并说明理由。
2. **验证**：把结构体放进你选定的目录后，运行两个守卫脚本确认通过：
   ```bash
   ./ci/check_config_boundary.sh
   ./ci/check_sw_sim_boundary.sh
   ```
3. **对比**：解释为什么不能图省事直接把它写进 `VX_config.toml` 当成一个 `[[param]]`（提示：那会让它变成 HW/sim 私有，软件侧 `sw/runtime` 就合法读不到了）。

**参考结论**：应放进 `sw/common/`（如已有的 `dxa_meta.h`、`gfx_sw_abi.h` 就是同类在线 ABI 结构体）。放 `sw/runtime/include/` 会让 `sim`/`hw` 反向违规；放 `hw/rtl/` 会让 `sw/runtime` 正向违规；写进 `VX_config.toml` 则把它判为私有构建配置，软件无法合法共享。

## 6. 本讲小结

- Vortex 把配置产物分成两个可见性等级：`VX_config`（HW/sim 私有构建配置）与 `VX_types`（软硬件共享 ABI 契约）；真正的 HW↔SW 契约（内存映射、页表格式）被特意从 config 搬进 types，使公共头无需碰私有配置。
- 公共运行时头 `vortex2.h` 刻意自包含，只 include 标准头；软件拿硬件参数走四条合法通道——`VX_types.h`、`vx_device_query()`、`config.mk`、`gen_config.py --cflags`——唯独不 `#include "VX_config.h"`。
- 守卫 ① `check_config_boundary.sh` 只盯一件事：`sw/` 与 `tests/` 里有没有 `#include "VX_config.h"`，一条 `grep` 正则定生死。
- 守卫 ② `check_sw_sim_boundary.sh` 强制 `sw/{kernel,runtime}` 与 `sim/`+`hw/` 的**双向**隔离，且 `#include` 与 Makefile `-I` 两条路都堵；`sw/runtime/opae` 因 FPGA AFU 绑定而显式豁免。
- `sw/common/` 是唯一合法的跨层共享通道——四个层都能访问、永不安装，且不在任何守卫的扫描范围内。
- 两个守卫都在 `ci/regression.sh` 最开头、`set -e` 下运行，是机械执行的硬门禁，而非靠自觉。

## 7. 下一步学习建议

- 本讲完结了 U2「硬件配置系统」。接下来进入 **U3 软件栈：主机运行时与驱动**：u3-l1 会从 `vortex.h`/`vortex2.h` 的公开 API 切入——你会再次看到 `vx_device_query()`，这次是从「主机程序如何编排一次 kernel 启动」的角度。
- 想加深对「软件如何不 include 配置却用上配置值」的理解，可先回顾 u2-l2 的 `--cflags` 腿，再看任一 `tests/regression/*` 的 Makefile 里 `-DVX_CFG_*` 标志是如何由 `common.mk` 投影出来的。
- 若你对「RTL 同时吃两份生成头」感兴趣，可提前浏览 [hw/rtl/VX_define.vh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/VX_define.vh) 里那些**派生宏**（如 `TCU_META_ENABLE`、`EXT_GFX_ANY_ENABLE`），它们是不带 `VX_CFG_` 前缀的内部量，对应 u2-l1 讲过的「派生量不带前缀」规则。
