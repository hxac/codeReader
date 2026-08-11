# u3-l2 utils_pkg 工具函数与 vhdl_utils 子模块

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `use work.utils_pkg.all;` 引用的 `utils_pkg` 包**并不在本仓库内**，而是来自外部 git 子模块 `ip/vhdl_utils`（对应 GitHub 上的 VHDL-Utils 仓库）。
- 解释 git submodule 的「仓库里嵌套另一个仓库」机制，以及为何 `clone` 后必须 `git submodule update --init` 才能编译/仿真。
- 读懂并手算 `to_bits`（计算「表示一个自然数最少需要多少位」）在 FIFO 地址位宽、计数位宽、generic 范围约束这三类场景中的用途。
- 读懂 `get_lowest_active_bit`（找出向量中最低有效位）在 SPI 多片选轮询中的作用。
- 看懂 `.gitmodules` 如何声明子模块、`test_runner.py` 如何通过 Python 导入路径依赖子模块。

## 2. 前置知识

本讲建立在以下已学内容之上（不再重复细节）：

- **package / package body 与 work 库**（u3-l1）：`memories_pkg` 教过 VHDL 包是「对外接口 + 内部实现」的单一真相源，别的文件用 `use work.<pkg>.all;` 复用，`work` 是默认库。
- **同一实体多架构模式**（u2-l1）：库里有 xilinx / intel / own 三套实现，本讲引用的 `fifo_sync.vhd` 正是多架构的代表。
- **本地仿真运行**（u1-l3）：`test_runner.py` 是 VUnit 的薄包装器，构成 `test_runner.py → run_all_testbenches_lib → VUnit → 仿真器` 的调用链；且 `clone` 后必须先初始化子模块，否则 `test_runner.py` 导入即报 `ModuleNotFoundError`。

一个关键直觉：VHDL 工程里「一个包的源码」并不一定要和你正在写的代码放在同一个 git 仓库。本库把通用工具函数抽到了一个**独立维护、独立版本号**的外部仓库，再用 git submodule 把它「挂载」进来。本讲就讲清楚这条挂载链，以及挂载进来后能用到哪些函数。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`ip/memories/fifo/fifo_sync.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) | 同步 FIFO，三套架构 | `to_bits` 的三类典型用法 |
| [`ip/memories/fifo/fifo_async.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd) | 异步 FIFO | `to_bits` 在 generic 范围约束与计数位宽中的用法 |
| [`ip/communication/spi/spi_interface.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd) | SPI 顶层接口 | `get_lowest_active_bit` 在多片选轮询中的用法 |
| [`ip/debouncer/debouncer.vhd`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd) | 消抖器 | `to_bits(natural'high)` 作 generic 上界 |
| [`.gitmodules`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.gitmodules) | git 子模块声明 | 子模块的 path / url 映射 |
| [`ip/test_runner.py`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py) | 仿真入口包装器 | Python 导入路径对子模块的依赖 |

> 说明：`utils_pkg`（含 `to_bits` / `get_lowest_active_bit` 的实现）位于外部子模块 `ip/vhdl_utils`（VHDL-Utils 仓库），**本仓库未检入其源码**，因此本仓库内读不到 `utils_pkg.vhd`。下文凡涉及包体内部实现处均标注「待确认（位于子模块）」，我们只依据本仓库的**调用点**来严谨推导其行为契约。

## 4. 核心概念与源码讲解

### 4.1 git submodule 机制：utils_pkg 住在另一个仓库

#### 4.1.1 概念说明

「可复用 IP 核库」天然会沉淀出一批与具体 IP 无关的通用函数（求位宽、找最低有效位……）。这些函数和具体的 FIFO、SPI 实现无关，更适合放进一个**独立仓库**单独维护、单独打版本号，再被多个工程共享。git submodule（子模块）就是实现「一个仓库里嵌套另一个仓库」的标准做法。

关键点：

- 子模块不是「把别人代码复制粘贴进来」，而是「在你的仓库里记一个**指针**，指向另一个仓库的某个具体 commit」。
- 你的仓库里只保存一条 gitlink（一个 commit 哈希）和一份 `.gitmodules` 描述，**不保存**子模块的工作树内容。
- 因此 `git clone` 本仓库后，子模块目录**默认是空的**，必须再用一条命令把那个 commit 的内容拉下来。

本库的子模块指向作者自己的工具仓库：

```
ip/vhdl_utils  →  https://github.com/nselvara/VHDL-Utils.git
```

`utils_pkg`、`tb_utils`、`run_all_testbenches_lib`（u1-l3 提到的仿真包装库）都来自这里。

#### 4.1.2 核心流程

子模块的生命周期可以分成三步：

```text
1. 声明   :  .gitmodules 记录  子模块名 → (path, url)
2. 检出   :  git clone 本仓库后，path 目录为空（只有一条 gitlink）
3. 初始化 :  git submodule update --init   ← 按 url 拉取并 checkout 指定 commit
            （此后 path 目录才出现真实的 .vhd / .py 文件）
```

对应到日常开发：

| 你做的事 | 子模块目录状态 | 能否编译 |
| --- | --- | --- |
| `git clone <本仓库>` | `ip/vhdl_utils/` **空** | 否（缺 `utils_pkg.vhd`） |
| `git submodule update --init` | `ip/vhdl_utils/` **被填充** | 能 |
| `git clone --recursive <本仓库>` | 一并填充 | 能 |

#### 4.1.3 源码精读

子模块的声明只有一行三元组（子模块名 / path / url）：

[`.gitmodules:1-3`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.gitmodules#L1-L3) 声明 `ip/vhdl_utils` 指向 VHDL-Utils 仓库。

> 这就是「本仓库不持有 utils_pkg 源码」的全部官方证据——它只是别人仓库的一个挂载点。你可以在本地确认：未经初始化时 `ip/vhdl_utils/` 目录里没有任何 `.vhd` 文件（待本地验证）。

子模块不仅提供 VHDL 包，还提供 Python 层的仿真包装库。入口脚本对它的依赖是硬编码的导入：

[`ip/test_runner.py:16`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L16) 写着 `from vhdl_utils.run_all_testbenches_lib import main ...`——这条 Python 导入路径 `vhdl_utils` 正对应子模块目录名。子模块没初始化时，这一行就会抛 `ModuleNotFoundError`（这正是 u1-l3 强调过的「必须先 init 子模块」的根因）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「未初始化的子模块目录是空的」，再用一条命令把它填上。

**操作步骤**：

1. 在本仓库根目录执行 `ls ip/vhdl_utils`（或 `git ls-files ip/vhdl_utils`），观察输出。
2. 执行 `git submodule update --init`。
3. 再次 `ls ip/vhdl_utils`，观察输出。
4. 执行 `git submodule status`，观察它显示的 commit 哈希与路径。

**需要观察的现象**：

- 第 1 步：目录为空（`git ls-files` 也不会列出本仓库检入的 `.vhd`，因为它们属于子模块）。
- 第 3 步：目录里出现了 `utils_pkg.vhd`、`tb_utils.vhd` 以及 `run_all_testbenches_lib.py` 等文件。
- 第 4 步：一行形如 `<commit-hash> ip/vhdl_utils (xxx)` 的输出，前面的哈希就是本仓库锁定的子模块版本。

**预期结果**：初始化后 `ip/vhdl_utils/utils_pkg.vhd` 存在，`test_runner.py` 的导入才会成功。若你在 CI/无网环境下无法联网拉取，则此步骤「待本地/待联网验证」。

#### 4.1.5 小练习与答案

- **练习**：为什么本仓库不直接把 `utils_pkg.vhd` 复制进来，而要用子模块？
  **参考答案**：复制会制造两份副本、改一处要改多处；子模块保证「工具函数只有一份真相源」，且可以独立打版本号、被多个工程共享，升级时只需在子模块里改一次、各工程各自 bump 指针。
- **练习**：同事说「我 clone 了你的仓库但仿真报 `ModuleNotFoundError: No module named 'vhdl_utils'`」，你会让他先做什么？
  **参考答案**：让他执行 `git submodule update --init`（或重新 `git clone --recursive`），把 `ip/vhdl_utils` 填充出来。

---

### 4.2 utils_pkg 包与 `use work.utils_pkg.all` 的解析

#### 4.2.1 概念说明

`utils_pkg` 在结构上和上一讲的 `memories_pkg` 是同类东西——一个 VHDL **package**（对外声明函数/类型）+ 可选的 **package body**（函数实现）。区别只在于它的源码不在本仓库，而在子模块里。

使用方式是熟悉的 `use work.utils_pkg.all;`：

- `work` —— 当前编译用的默认库（见 u3-l1）。
- `utils_pkg` —— 包名。
- `.all` —— 把包里**所有**对外可见内容都引入可见域。

这里有一个容易困惑的点：源码明明在 `ip/vhdl_utils/` 子目录下，为什么能被 `work.utils_pkg` 解析到？答案是——**VHDL 的「库」是编译期概念，不是磁盘目录**。仿真脚本在收集源文件时，会把 `ip/` 下（含子模块目录）的所有 `.vhd` 一并编译进 `work` 库；文件落在哪个子文件夹并不影响它最终进哪个库。所以子模块里的 `utils_pkg.vhd` 编译进 `work` 后，本仓库任意文件写 `use work.utils_pkg.all;` 就能拿到它。

#### 4.2.2 核心流程

从「源码文件」到「`use` 可见」的链路：

```text
ip/vhdl_utils/utils_pkg.vhd      ← 子模块提供（git submodule update --init 后才存在）
        │  test_runner 收集 ip/**/*.vhd 并编译
        ▼
   work 库  ──►  work.utils_pkg
        │  use work.utils_pkg.all;
        ▼
 fifo_sync.vhd / spi_interface.vhd / debouncer.vhd ... 都能用 to_bits / get_lowest_active_bit
```

这也就解释了 u1-l3 的现象：子模块没初始化 → 没有源文件被收集 → `work.utils_pkg` 不存在 → 所有写了 `use work.utils_pkg.all;` 的文件编译报错。

#### 4.2.3 源码精读

全库对 `utils_pkg` 的依赖非常广泛。仅看本讲引用的几个设计文件，依赖声明都集中在文件顶部：

[`ip/memories/fifo/fifo_sync.vhd:5`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L5)、[`ip/memories/fifo/fifo_async.vhd:13`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L13)、[`ip/communication/spi/spi_interface.vhd:32`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L32)、[`ip/debouncer/debouncer.vhd:14`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L14) 都是同一句 `use work.utils_pkg.all;`。

注意：和 u2-l1 讲过的「厂商库声明紧贴各 architecture 之前」不同，`utils_pkg` 是**厂商无关的纯 VHDL**，所以它的 `use` 写在 entity 之前的公共区，三套架构都能用。这是「包级共享」与「厂商局部化」两种依赖风格的对比。

> 旁注：测试台侧还有 `tb_utils`（同样来自子模块，下一讲 u3-l3 专讲），它只用于仿真，不参与综合。

#### 4.2.4 代码实践

**实践目标**：用一条命令量化「全库有多少文件依赖 utils_pkg」，体会它的「基础设施」地位。

**操作步骤**：

1. 在仓库根目录执行：`grep -rn "use work.utils_pkg.all" ip/`。
2. 数一数命中行数，并区分「设计文件 `.vhd`」与「测试台 `tb_*.vhd`」。

**需要观察的现象**：能同时看到设计侧（`debouncer.vhd`、`fifo_sync.vhd`、`spi_interface.vhd`、`spi_tx.vhd` …）与验证侧（`tb_spi_tx.vhd`、`tb_pll.vhd` …）的命中，说明这个包横跨设计与验证。

**预期结果**：命中应在十处上下（基于当前 HEAD 的统计）。如果某天这些命中里的某个文件被删，对应行自然消失——这正是「以真实源码为准」的体现。

#### 4.2.5 小练习与答案

- **练习**：`utils_pkg.vhd` 在 `ip/vhdl_utils/` 子目录，而 `memories_pkg.vhd` 在 `ip/memories/` 目录，两者最终都进了 `work` 库。这说明 VHDL「库」与「目录」是什么关系？
  **参考答案**：库是编译期逻辑容器，由仿真脚本的源文件收集与库映射决定，与磁盘目录无强绑定；同一库可由多个目录的文件共同构成。
- **练习**：把 `utils_pkg` 的 `use` 写在 entity 之前 vs 写在某个 architecture 之前，效果有何不同？
  **参考答案**：写在 entity 前属于公共区，三套 architecture 都可见（本库正是这么用，因为 utils_pkg 厂商无关）；写在某 architecture 前则只对该架构可见，适合厂商库那种需要局部化的依赖。

---

### 4.3 to_bits：计算「表示一个数需要多少位」

#### 4.3.1 概念说明

`to_bits` 是一个纯函数，返回「表示一个自然数最少需要多少个二进制位」。它是本库出现频率最高的工具函数，因为**位宽推导**是写可参数化 RTL 的家常便饭——FIFO 深度变了，地址线和计数线的位宽就得跟着变，手算极易出错，交给函数最稳妥。

数学定义（由调用点反推的契约）：

\[
\text{to\_bits}(n) = \lceil \log_2(n + 1) \rceil, \quad n \in \mathbb{N}
\]

典型取值：

| \(n\) | 二进制 | \(\text{to\_bits}(n)\) |
| --- | --- | --- |
| 0 | 0 | 0 或 1（待确认实现，下同） |
| 1 | 1 | 1 |
| 2 | 10 | 2 |
| 7 | 111 | 3 |
| 8 | 1000 | 4 |
| `natural'high`（\(2^{31}-1\)） | — | 31 |

> 实现细节（具体是函数还是循环、对 0 的处理）位于子模块 `utils_pkg.vhd`，**待确认**。下文只依据本仓库调用点的数学契约来推导行为。

#### 4.3.2 核心流程：三类典型用法

本库里 `to_bits` 的调用可归纳成三种模式，理解了这三类就掌握了它的全部用途：

```text
用法 A — 地址位宽  : to_bits(FIFO_DEPTH - 1)
                    「N 个字需要几位地址」→ 能编码 0..N-1
用法 B — 计数位宽  : to_bits(FIFO_DEPTH)
                    「计数值最大到 N，需要几位」→ 能容纳 0..N
用法 C — generic 上界 : natural range 0 to to_bits(natural'high)
                    「把 generic 限制在 0..31」→ 防止用户填入荒谬的位数
```

A 与 B 差 1 是关键区别：地址只要编到 `N-1`，而计数要能写到 `N`（满），所以计数比地址多一位的需求。下面逐一对应源码。

#### 4.3.3 源码精读

**用法 A：地址位宽**——自研同步 FIFO 用它推导读写指针的位宽。FIFO 默认深度 2，则 `to_bits(2 - 1) = to_bits(1) = 1`，1 位指针正好编址 0、1 两个字：

[`ip/memories/fifo/fifo_sync.vhd:148-151`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L148-L151)：`constant ADDR_WIDTH: natural := to_bits(FIFO_DEPTH - 1);` 随后指针 `write_pointer/read_pointer: unsigned(ADDR_WIDTH-1 downto 0)`。注意 `fifo_fill_level` 用的是 `ADDR_WIDTH downto 0`（多一位），多出来的最高位正是满/空判定的「折回位」（详见 u9 单元）。

异步 FIFO 同样用它定地址宽：

[`ip/memories/fifo/fifo_async.vhd:40-41`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L40-L41)：`FIFO_DEPTH := 2**FIFO_DEPTH_IN_BITS;` 再 `ADDRESS_WIDTH := to_bits(FIFO_DEPTH);`（这里地址宽取 `to_bits(FIFO_DEPTH)` 是 Cummings 异步 FIFO 的指针惯例，u9-l3 会详述）。

**用法 B：计数位宽**——给厂商 FIFO 的「已用字数」信号定线宽。Intel `scfifo` 的 `usedw`、`dcfifo` 的位宽由 `lpm_widthu` 指定：

[`ip/memories/fifo/fifo_sync.vhd:108`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L108) 声明 `words_stored_slv: std_ulogic_vector(to_bits(FIFO_DEPTH) - 1 downto 0);`，再在 [`:119`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L119) 把 `lpm_widthu => to_bits(FIFO_DEPTH)` 传给 `scfifo`。Xilinx 侧同理，[`fifo_sync.vhd:36`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L36) 用 `to_bits(FIFO_DEPTH) - 1` 给 `wr_data_count` 定宽。异步 FIFO 在 [`fifo_async.vhd:157`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L157) 同样写 `lpm_widthu => to_bits(FIFO_DEPTH)`。

**用法 C：generic 上界**——把「以位为单位的」generic 限制到一个合理范围。`natural'high` 是 `natural` 的最大值（\(2^{31}-1\)），`to_bits(natural'high) = 31`：

[`ip/memories/fifo/fifo_async.vhd:18`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L18)：`FIFO_DEPTH_IN_BITS: natural range 0 to to_bits(natural'high) := 2;`
[`ip/debouncer/debouncer.vhd:18`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L18)：`DEBOUNCE_SYNC_BITS: natural range 0 to to_bits(natural'high) := 10;`

这两处等价于 `natural range 0 to 31`：因为这两个 generic 的语义是「位数」（FIFO 深度位数、消抖计数器位数），超过 31 位没有物理意义，于是用 `to_bits(natural'high)` 给出一个与平台位宽自洽的上界——既防止用户填入 `1000000` 这种荒谬值，又不必硬编码魔术数字 31。

#### 4.3.4 代码实践

**实践目标**：手算 `to_bits` 在三类用法下的真实取值，验证你对位宽推导的理解。

**操作步骤**：

1. 假设例化 `fifo_sync` 时把 `FIFO_DEPTH` 设为 `8`。
2. 对用法 A：计算 `to_bits(FIFO_DEPTH - 1) = to_bits(7)`，写出 `ADDR_WIDTH`、指针位宽、可编址字数。
3. 对用法 B：计算 `to_bits(FIFO_DEPTH) = to_bits(8)`，写出 `words_stored_slv` 的线宽、`usedw` 能表示的最大计数值。
4. 对用法 C：把 `FIFO_DEPTH_IN_BITS` 改为 `5`，计算最终 `FIFO_DEPTH = 2**5` 与 `ADDRESS_WIDTH = to_bits(32)`。

**需要观察的现象 / 预期结果（手算）**：

| 设定 | 表达式 | 结果 | 含义 |
| --- | --- | --- | --- |
| A, FIFO_DEPTH=8 | `to_bits(7)` | 3 | 3 位地址编址 0..7，正好 8 个字 |
| B, FIFO_DEPTH=8 | `to_bits(8)` | 4 | 4 位计数可容纳 0..8（含「满」=8） |
| C, IN_BITS=5 | `2**5` / `to_bits(32)` | 32 / 6 | FIFO 深 32 字，地址需 6 位（编址 0..31 需 to_bits(31)=5？注意这里是 `to_bits(FIFO_DEPTH)=to_bits(32)=6`，与同步 FIFO 的 `to_bits(DEPTH-1)` 取法不同，u9 会讲） |

第 4 行特意暴露一个细节：异步 FIFO 取 `to_bits(FIFO_DEPTH)`、同步 FIFO 取 `to_bits(FIFO_DEPTH - 1)`，差 1 的原因（格雷码指针多一位折回位）留到 u9-l3 揭晓，这里先建立「同一函数、不同调用点、语义有别」的敏感度。若你在本地有仿真器，可例化一版并在地址线上观察宽度，否则记为「待本地验证」。

#### 4.3.5 小练习与答案

- **练习**：为何 `to_bits(FIFO_DEPTH - 1)` 与 `to_bits(FIFO_DEPTH)` 在 FIFO_DEPTH=8 时一个是 3、一个是 4？
  **参考答案**：地址只需编到 `N-1=7`，`to_bits(7)=3`；计数要能表示满值 `N=8`，而 `8=1000_2` 需 4 位，`to_bits(8)=4`。
- **练习**：`natural range 0 to to_bits(natural'high)` 比直接写 `natural range 0 to 31` 好在哪？
  **参考答案**：前者随 `natural` 的平台位宽自洽（`natural'high` 变了，上界自动跟着变），不依赖人脑记住「31 这个魔术数」，可读性与可移植性更好。

---

### 4.4 get_lowest_active_bit：找出最低有效位

#### 4.4.1 概念说明

`get_lowest_active_bit` 接收一个 `std_ulogic_vector`，返回其中**值最靠低的那个有效位（'1'）的索引**。它是「优先级编码器」的一种简化语义：当多位同时有效时，固定选择编号最小的那一位。

直觉例子（设向量下标 `downto 0`）：

| 输入向量（高位…低位） | 最低有效位索引 |
| --- | --- |
| `"0001"` | 0 |
| `"1010"` | 1 |
| `"0100"` | 2 |
| `"1000"` | 3 |
| `"0000"` | 无有效位（待确认：实现可能返回某哨兵值或 0，子模块内实现待确认） |

> 实现细节位于子模块 `utils_pkg.vhd`，**待确认**；本仓库只用到「有有效位」的分支。

在本库，它服务的是 SPI 控制器的**多片选轮询**：当用户同时选中多片从机时，控制器要「一次只服务一片」，并且总是从编号最低的那片开始。

#### 4.4.2 核心流程

SPI 顶层接口用一个有限状态机轮询片选（u10-l4 会精讲整个 FSM）。决定「下一片选谁」的逻辑封装在一个 `impure function get_next_selected_chip` 里，其入口就用 `get_lowest_active_bit`：

```text
get_next_selected_chip(state):
  if 当前已越界 (current_chip_index >= CHIP_INDEX_OUT_OF_RANGE):
      return get_lowest_active_bit(selected_chips)   # 回到最低有效片，重新开始一轮
  else:
      从当前位置往后扫，找下一个 '1' 的位
```

也就是说，「越界后从头再来」这件事，正是靠 `get_lowest_active_bit` 把指针重置到最低有效片实现的。

#### 4.4.3 源码精读

调用点在 SPI 顶层接口的状态机进程内：

[`ip/communication/spi/spi_interface.vhd:103-115`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L103-L115) 定义了 `impure function get_next_selected_chip return natural`，其中 [`:105`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L105) 的 `return get_lowest_active_bit(selected_chips);` 就是「重新定位到最低有效片」的关键一行。

理解上下文需要两个量（同文件上方定义）：

- [`spi_interface.vhd:50`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L50)：`selected_chips: in std_ulogic_vector(SPI_CHIPS_AMOUNT - 1 downto 0)`——每位代表一片从机是否被选中。
- [`spi_interface.vhd:75`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L75)：`constant CHIP_INDEX_OUT_OF_RANGE: natural := selected_chips'length;`——把「片数」当作越界哨兵。

于是整个轮询的语义是：`get_next_selected_chip` 一次只推进一个有效片；当遍历到越界（`>= CHIP_INDEX_OUT_OF_RANGE`）时，调用 `get_lowest_active_bit` 跳回最低有效片，开始新一轮。这套机制配合 FIFO 的 `reset_read_pointer`（u10-l4）就能让「同一批数据被逐片重放给多片从机」。

> 注意 `get_next_selected_chip` 被声明为 `impure function`——因为它读取了进程可见的信号 `selected_chips`（非参量）。这是 VHDL 里「函数读外部信号」时的必要修饰，初学者可暂记为「读外部信号 → 用 impure」。

#### 4.4.4 代码实践

**实践目标**：手推 `get_lowest_active_bit` 在几种 `selected_chips` 取值下的返回值，并把它与 SPI 轮询语义对上。

**操作步骤**：

1. 假设 `SPI_CHIPS_AMOUNT = 4`，则 `selected_chips` 是 4 位向量（位 3..0）。
2. 对下面 4 组输入，分别写出 `get_lowest_active_bit` 的返回值，并说明「下一片会选谁」。

| `selected_chips` | `get_lowest_active_bit` 返回 | 选中第几片 |
| --- | --- | --- |
| `"0001"` | ? | ? |
| `"0011"` | ? | ? |
| `"1010"` | ? | ? |
| `"1000"` | ? | ? |

**需要观察的现象 / 预期结果**：返回值分别是 0、0、1、3——即总是选编号最低的有效片；`"0011"` 同时选中片 0 和片 1，但最先服务片 0。

**进阶（结合源码）**：在 [`spi_interface.vhd:103-115`](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L103-L115) 里跟踪：当 `current_chip_index_v >= CHIP_INDEX_OUT_OF_RANGE` 时走 `get_lowest_active_bit` 分支（回到最低片），否则走 `for` 循环向后找下一个有效位。请用一句话描述这两条分支如何配合实现「轮询所有有效片、转完一圈回到起点」。

#### 4.4.5 小练习与答案

- **练习**：为什么不直接用二进制优先级编码器，而要把语义命名成 `get_lowest_active_bit`？
  **参考答案**：函数名直接表达业务意图「取最低有效片」，调用处 `return get_lowest_active_bit(selected_chips)` 一眼读懂；命名即文档，比裸优先级编码器更可维护。
- **练习**：如果 `selected_chips = "0000"`（无任何从机被选中），调用 `get_lowest_active_bit` 会怎样？
  **参考答案**：无有效位，行为取决于子模块实现（待确认）。从 SPI FSM 的角度，正常使用中 `tx_trigger` 之前用户应至少选中一片；全零属于边界情况，需结合 FSM 的 idle 状态保护（u10-l4）。

---

## 5. 综合实践

把本讲三条主线串起来：**子模块来源 → 包导入 → 两个函数的真实调用点**。

**任务**：绘制一张「函数 → 使用它的模块」依赖图，并解释「clone 后必须 `git submodule update --init` 才能编译」的完整因果链。

**操作步骤**：

1. 在仓库根目录执行下面两条命令，统计两个函数在全库 `ip/` 下的所有调用点：
   ```bash
   grep -rn "to_bits" ip/ --include=*.vhd
   grep -rn "get_lowest_active_bit" ip/ --include=*.vhd
   ```
2. 把命中整理成两张表（参考下方「预期结果」），每行是「文件 : 行号 : 调用表达式」。
3. 把两张表合成一张依赖图：`utils_pkg` 居中，`to_bits` / `get_lowest_active_bit` 作为两条出边，连到各自的使用模块。
4. 用一条因果链回答：为什么 `git clone` 完直接编译会失败？写出从「子模块未初始化」到「编译报错」的每一步。

**预期结果（基于当前 HEAD 45eae77 的源码统计）**：

- `to_bits` 的设计侧调用点至少包括：
  - `ip/memories/fifo/fifo_sync.vhd:36`（Xilinx 计数位宽）、`:108`（Intel usedw 位宽）、`:119`（`lpm_widthu`）、`:148`（自研地址宽）
  - `ip/memories/fifo/fifo_async.vhd:18`（generic 上界）、`:41`（ADDRESS_WIDTH）、`:157`（`lpm_widthu`）
  - `ip/debouncer/debouncer.vhd:18`（generic 上界）
  - `ip/memories/fifo/tb/tb_fifo_async.vhd:793`（测试台 generic 映射）
- `get_lowest_active_bit` 的调用点：`ip/communication/spi/spi_interface.vhd:105`（多片选轮询）。

**因果链（参考答案）**：

```text
git clone 本仓库（未带 --recursive）
   → ip/vhdl_utils/ 目录为空（本仓库只持 gitlink，不持子模块内容）
   → 没有 utils_pkg.vhd 被编译进 work 库
   → work.utils_pkg 不存在
   → 所有 use work.utils_pkg.all; 的文件（fifo_sync / spi_interface / debouncer …）编译失败
   → 同时 test_runner.py 第 16 行 from vhdl_utils... 也 ModuleNotFoundError
修复：git submodule update --init → 子模块填充 → 编译通过
```

> 若你本地暂无 VHDL 工具链，步骤 1–3 的 grep 与绘图可在纯文本环境完成，步骤 4 的编译验证记为「待本地/待联网验证」。

## 6. 本讲小结

- `utils_pkg` 不在本仓库，它来自外部 git 子模块 `ip/vhdl_utils`（VHDL-Utils 仓库），由 `.gitmodules` 声明。
- `clone` 后必须 `git submodule update --init`（或 `clone --recursive`），否则 `ip/vhdl_utils/` 为空，既导致 `use work.utils_pkg.all;` 编译失败，也让 `test_runner.py` 的 Python 导入报 `ModuleNotFoundError`。
- 「目录 ≠ 库」：子模块里的 `utils_pkg.vhd` 被仿真脚本编译进 `work` 库后，全库任意文件都能用 `use work.utils_pkg.all;` 访问。
- `to_bits(n)` 返回表示 `n` 所需的最少位数，在本库有三类用法：地址位宽 `to_bits(FIFO_DEPTH-1)`、计数位宽 `to_bits(FIFO_DEPTH)`、generic 上界 `to to_bits(natural'high)`（≈ 0..31）。
- `get_lowest_active_bit(vec)` 返回向量中最低有效位的索引，在 SPI 顶层接口里用于多片选轮询的「回到最低有效片」。
- 两个函数的具体实现都在子模块 `utils_pkg.vhd` 内（本仓库未检入），相关实现细节标注为待确认；本讲只依据真实调用点推导其行为契约。

## 7. 下一步学习建议

- 沿着「子模块还提供了什么」继续：下一讲 **u3-l3** 会讲同样来自 `ip/vhdl_utils` 的验证侧包 `tb_utils`（如 `generate_advanced_clock` 并发过程），理解「设计侧 utils_pkg / 验证侧 tb_utils」的边界。
- 想看 `to_bits` 在真实电路里的体现，进入 **u9（FIFO 设计）**：`to_bits` 推导出的指针位宽与「满/空折回位」直接相关，u9-l1（同步 FIFO）和 u9-l3（异步 FIFO 格雷码指针）会用到本讲的位宽推导结论。
- 想看 `get_lowest_active_bit` 的完整业务场景，进入 **u10-l4（SPI 顶层接口 FSM）**：本讲只看了 `get_next_selected_chip` 一处，u10-l4 会把六状态 FSM 和 FIFO 重放机制讲透。
- 动手建议：在本地初始化子模块后，打开 `ip/vhdl_utils/utils_pkg.vhd`，把 `to_bits` / `get_lowest_active_bit` 的真实实现读一遍，回头把本讲里标注「待确认」的地方补上准确描述。
