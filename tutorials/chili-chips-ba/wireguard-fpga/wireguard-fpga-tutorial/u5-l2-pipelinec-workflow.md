# PipelineC HLS 工作流与三种设计变体

## 1. 本讲目标

在上一讲（u5-l1）里，我们已经建立了 ChaCha20-Poly1305 AEAD 的密码学心智模型。本讲换一个视角，不再讲「密码算法算什么」，而是讲「这段 C 加密代码是怎么变成 FPGA 上的 Verilog 硬件的」。

学完本讲，你应当能够：

- 说清 **PipelineC** 这套开源 HLS（高层次综合）工具的工作流：一条 `$PIPELINEC ... --verilog` 命令如何把 C 编译成可综合 Verilog。
- 区分本项目的 **三种设计变体**（独立加密、独立解密、加密解密共享），知道它们的源文件、生成产物与适用场景的差异。
- 读懂 PipelineC 的 **pragma 与实例化约定**：`MAIN_MHZ`/`PART`/`FEEDBACK`、`DECL_INPUT`/`DECL_OUTPUT`、`SIMULATION` 宏、以及 `#define` + `#include` 的模块多实例化模式。
- 跑一次 `build_verilog.sh`（或阅读预生成产物），在生成的 Verilog 里定位**顶层端口**与 **axis128↔axis512 位宽转换**的位置。

## 2. 前置知识

- **HLS（High-Level Synthesis，高层次综合）**：传统 FPGA 开发用 Verilog/VHDL 描述寄存器传输级（RTL），手写很繁琐。HLS 让你用 C/C++ 写算法，由工具自动调度、绑定，生成等价的 RTL。本项目用的 PipelineC 是一种「把 C 函数直接编译成流水线 Verilog」的开源 HLS，思路接近 Xilinx Vitis HLS，但语法更贴近普通 C。
- **pragma**：C 里的 `#pragma` 是「给编译器的提示」，标准 C 会忽略它，但 PipelineC 编译器会识别这些专属 pragma 来决定时钟频率、目标器件、顶层模块等。可以理解为「写在 C 里的硬件配置开关」。
- **AXI-Stream（AXIS）**：一种流式握手接口，靠 `tvalid`/`tready` 同拍为 1 完成一个数据节拍（beat），`tdata` 是数据、`tkeep` 是字节使能、`tlast` 标记包尾。本项目的数据面主线就是 128 位的 AXIS（回顾 u4-l1）。
- **位宽转换（width conversion）**：ChaCha20 一次处理一个 64 字节 = 512 位的块，但总线只有 128 位宽，因此需要把 4 个 128 位 beat 拼成 1 个 512 位块送进运算核，出来再拆回去。
- 建议先读完 u5-l1，确认你理解「加密 = ChaCha20 加密 + Poly1305 算认证 tag」这条链。

## 3. 本讲源码地图

本讲聚焦 `3.build/pipelinec_build/` 目录，它是整个加密硬件的「C 源头」与「Verilog 生成器」。

| 文件 | 作用 |
|---|---|
| `README.md` | 加解密各功能块的架构说明（含框图），讲清每个 FSM 的状态 |
| `CLAUDE.md` | 给开发者的精简备忘录：构建命令、三种变体对照表、关键约定 |
| `build_verilog.sh` / `build_verilog_decrypt.sh` / `build_verilog_shared.sh` | 三条 shell 脚本，分别对应三种设计变体，调用 `$PIPELINEC` 生成 Verilog |
| `src/chacha20poly1305_encrypt.c` | **独立加密**变体的顶层组装文件（本讲主线） |
| `src/chacha20poly1305_decrypt.c` | **独立解密**变体的顶层组装文件 |
| `src/chacha20poly1305_encrypt_decrypt_shared.c` | **共享**变体的顶层组装文件 |
| `src/chacha20poly1305/encrypt_dataflow.c` | 加密数据流的「连线函数」，用 `#pragma MAIN_MHZ` 标记 |
| `src/chacha20poly1305/chacha20poly1305_encrypt.h` | 顶层端口声明：`DECL_INPUT`/`DECL_OUTPUT` 与 `SIMULATION` 守卫 |
| `src/chacha20/chacha20.c` | `#define CHACHA_INST` 多实例化模式的范例 |
| `generated-files/chacha20poly1305_encrypt.v` | 脚本预生成并提交进仓库的 Verilog 产物（顶层模块在此） |

> 提醒：与 u4 一致，当前 HEAD 处于 Phase1 PoC，PipelineC 生成的 `generated-files/*.v` 目前**并未**登记进 `1.hw/top.filelist`（该 filelist 只引用了 `csr_build/generated-files/`）。也就是说加密核源码已写好、能生成 Verilog，但还没焊进当前 bitstream 的数据面。本讲关注的是「如何用 PipelineC 生成它」，至于它何时上线见 u5-l3/u5-l4。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲 PipelineC 的整体 HLS 流程（4.1），再讲它产出的三种设计变体（4.2），最后讲贯穿三者的 pragma 与实例化约定（4.3）。

### 4.1 PipelineC HLS 流程

#### 4.1.1 概念说明

PipelineC 是一个开源 HLS 编译器（[GitHub: JulianKemmerer/PipelineC](https://github.com/JulianKemmerer/PipelineC)）。它的核心思想是：**把每个 C 函数编译成一条流水线**——函数体内的组合逻辑被拆成一拍一拍的级，函数的「调用」变成数据在流水线里流动。它直接输出可综合的 Verilog，不依赖任何厂商专有工具。

本项目的 README 开宗明义：加密与解密都是用 PipelineC 搭的。

> 加解密均使用 PipelineC 构建（[README.md:7-9](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/README.md#L7-L9)）。

使用前必须把环境变量 `$PIPELINEC` 指向 PipelineC 可执行文件（[CLAUDE.md:9](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/CLAUDE.md#L9)），所有构建脚本都靠它驱动。

#### 4.1.2 核心流程

一条 HLS 构建的典型流程如下：

```text
chacha20poly1305_encrypt.c (C 源 + pragma)
        │
        │  $PIPELINEC <src.c> --out_dir <dir> --top <name> --verilog
        ▼
generated-files-verilog/*.v  (可综合 Verilog，含顶层模块 + 所有子模块)
        │
        │  交给 Vivado 或 openXC7 综合/PnR
        ▼
FPGA 比特流
```

要点：

1. **入口是一个 C 文件**：它用 `#include` 把各功能块（chacha20、poly1305、prep_auth_data 等）拼到一起，再用 pragma 标注时钟与器件。
2. **`--top` 指定顶层模块名**：生成的 Verilog 顶层模块就叫这个名字。
3. **`--verilog` 表示生成硬件**（区别于 `--sim` 仿真模式）。
4. **产物是「扁平化」的**：PipelineC 会把所有用到的子函数都展开成独立的 Verilog 模块，一个 `.v` 文件里常包含成百上千个小模块。
5. **每次构建都从零重建**：脚本先 `rm -rf` 清空输出目录再生成，没有增量编译（[CLAUDE.md:13](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/CLAUDE.md#L13)）。

#### 4.1.3 源码精读

加密变体的生成脚本只有三行实质内容：

> [build_verilog.sh:7-9](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/build_verilog.sh#L7-L9) —— 先清空 `generated-files-verilog/`，删除临时 `.py`，再用 `$PIPELINEC` 把 `chacha20poly1305_encrypt.c` 编译成 Verilog，顶层模块名为 `chacha20poly1305_encrypt`。

```bash
rm -rf ./generated-files-verilog/*
rm ./*.py
$PIPELINEC ./src/chacha20poly1305_encrypt.c --out_dir ./generated-files-verilog --top chacha20poly1305_encrypt --verilog
```

这条命令的输入 `chacha20poly1305_encrypt.c` 是「顶层组装文件」，它的全部职责就是把零件拼起来并贴上器件标签（详见 4.3）：

> [src/chacha20poly1305_encrypt.c:26-27](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_encrypt.c#L26-L27) —— `#pragma PART` 把设计绑定到 Artix-7 200T 器件，随后 `#include` 进真正的数据流连线函数。

```c
#pragma PART "xc7a200tffg1156-2" // Artix 7 200T
#include "chacha20poly1305/encrypt_dataflow.c"
```

脚本运行后，产物落入 `generated-files-verilog/`；仓库里另有一份**预生成并提交**的同款产物在 `generated-files/` 目录，供 Vivado/openXC7 直接取用（[CLAUDE.md:32](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/CLAUDE.md#L32)）。即使你本机没装 PipelineC，也能直接读这份 `generated-files/chacha20poly1305_encrypt.v` 看到结果。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍 HLS 生成流程，观察「C 进、Verilog 出」。

**操作步骤**：

1. 确认环境变量已设置：`echo $PIPELINEC`（应指向 PipelineC 可执行文件）。
2. 进入目录：`cd 3.build/pipelinec_build`。
3. 执行：`./build_verilog.sh`。
4. 若未安装 PipelineC，则跳过运行，直接阅读已提交的产物 `generated-files/chacha20poly1305_encrypt.v`——它就是这条脚本的输出。
5. 用编辑器打开生成的 `chacha20poly1305_encrypt.v`，搜索顶层模块声明。

**需要观察的现象**：

- `generated-files-verilog/` 下出现一个（或几个）`.v` 文件。
- 文件顶部是大量「零件级」小模块（`bin_op_and_*`、`axis128_to_axis512_*` 等），顶层模块 `chacha20poly1305_encrypt` 出现在文件靠后位置。

**预期结果**：顶层模块声明形如（来自预生成产物 [generated-files/chacha20poly1305_encrypt.v:17727](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/generated-files/chacha20poly1305_encrypt.v#L17727)）：

```verilog
module chacha20poly1305_encrypt(clk_80p0, encrypt_key, encrypt_nonce, encrypt_aad,
  encrypt_aad_len, encrypt_s_axis_tdata, encrypt_s_axis_tkeep, encrypt_s_axis_tlast,
  encrypt_s_axis_tvalid, encrypt_m_axis_tready, encrypt_s_axis_tready,
  encrypt_m_axis_tdata, encrypt_m_axis_tkeep, encrypt_m_axis_tlast, encrypt_m_axis_tvalid);
```

端口含义（呼应 4.3 的 `DECL_INPUT/OUTPUT`）：

| 端口 | 方向 | 含义 |
|---|---|---|
| `clk_80p0` | input | 80 MHz 时钟（由 `MAIN_MHZ 80.0` 生成） |
| `encrypt_key` / `encrypt_nonce` / `encrypt_aad` / `encrypt_aad_len` | input | 密钥、nonce、AAD 等配置 |
| `encrypt_s_axis_t*` | input（tready 为 output） | AXIS **从机(subordinate)** 口：输入明文流 |
| `encrypt_m_axis_t*` | output（tready 为 input） | AXIS **主机(manager)** 口：输出密文+tag 流 |

> 若你未实际运行脚本，以上端口清单即为「待本地验证」的参考——它是已提交产物的真实内容，可直接核对。

#### 4.1.5 小练习与答案

**练习 1**：`build_verilog.sh` 里 `rm ./*.py` 删的是什么？为什么需要删？

**答案**：PipelineC 运行时会在当前目录生成一些中间 Python 辅助脚本（用于内部调度/解析），`rm ./*.py` 清理上一次运行残留，避免污染本次生成。这与「清空输出目录」一样，都是为保证「从零重建」的干净起点。

**练习 2**：为什么 `--top chacha20poly1305_encrypt` 这个名字很重要？

**答案**：它直接决定了生成 Verilog 的顶层模块名。后续 Vivado/openXC7 综合、约束文件（`generated-files/chacha20poly1305_encrypt.xdc`）以及上层例化都要按这个名字来引用，名字错了整条工具链对不上。

---

### 4.2 三种设计变体

#### 4.2.1 概念说明

同一个 ChaCha20-Poly1305 算法，本项目准备了三套不同的硬件实现，对应三个顶层 C 文件、三条构建脚本、三套生成产物。它们的区别不在「密码学正确性」（都符合 RFC8439），而在**面积与吞吐的取舍**：

| 变体 | 顶层源文件 | 构建脚本 | 输出目录 | 特点 |
|---|---|---|---|---|
| 独立加密 | `chacha20poly1305_encrypt.c` | `build_verilog.sh` | `generated-files-verilog/` | 只做加密，独占一条 ChaCha20 流水线 |
| 独立解密 | `chacha20poly1305_decrypt.c` | `build_verilog_decrypt.sh` | `generated-files-verilog-decrypt/` | 只做解密，多了 strip/verify/wait 三个块 |
| 加密解密共享 | `chacha20poly1305_encrypt_decrypt_shared.c` | `build_verilog_shared.sh` | `generated-files-verilog-shared/` | 加解密**共用一条** ChaCha20 流水线，省面积 |

CLAUDE.md 给出了对照表：

> [CLAUDE.md:38-42](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/CLAUDE.md#L38-L42) —— 三种变体：独立加密、独立解密、加解密共享一条 ChaCha20 流水线（省面积）。

为什么要做三套？因为加密和解密的核心计算（生成 ChaCha20 密钥流）**完全相同**，而 ChaCha20 流水线又是整个设计里最贵、最深的资源（64 级）。于是出现了两种合法选择：

- **不共享**：加密、解密各 instantiate 一条流水线，吞吐高、面积大。
- **共享**：只 instantiate 一条流水线，用调度机制把两路请求复用进去，面积小、峰值吞吐减半。

#### 4.2.2 核心流程

**独立加密**的数据流（回顾 u5-l1，承接 u5-l3 详讲）：

```text
plaintext → chacha20 → [密文分叉]
                       ├─→ append_auth_tag（密文直通输出）
                       └─→ prep_auth_data → poly1305_mac → auth_tag ─→ 追加为最后一拍
```

**独立解密**多出三个块，因为解密必须「先验 tag、通过才放行」：

```text
ciphertext+tag → strip_auth_tag → [密文分叉]
                                  ├─→ chacha20 → wait_to_verify ──→ plaintext
                                  └─→ prep_auth_data → poly1305_mac → poly1305_verify ─↗
```

`wait_to_verify` 是一个 128 字深的 FIFO，先把解密出的明文缓存住，等 tag 比对结果出来：通过才放行、失败则丢弃，防止未认证的明文流出芯片（详见 u5-l4）。

**共享变体**的关键机制：让一个 1 比特的 `is_encrypt` 标签**随数据一起穿过整条 64 级流水线**，输入端按 round-robin 轮流喂加密/解密请求，输出端按这个标签把结果**解复用**回各自的消费者。

```text
              ┌─────────────────────────────────────┐
encrypt_req ──┤MUX（round-robin，按is_encrypt选路）  │
              │   ┌───────────────────────────┐    │
              │   │  ChaCha20 流水线（64 级）  │    │  ← 只此一条
              │   │  每级携带 is_encrypt 标签  │    │
              │   └───────────────────────────┘    │
decrypt_req ──┤DEMUX（按输出带的is_encrypt分流）    │
              └─────────────────────────────────────┘
encrypt_result ←──────────────────────────────────── decrypt_result
```

数学上，共享后单条流水线的吞吐被两路瓜分。设流水线可稳态输出 \( T \) 个块/拍，则加密、解密各分得约：

\[
T_{\text{encrypt}} \approx T_{\text{decrypt}} \approx \frac{T}{2}
\]

代价是面积几乎减半（省掉一整条 64 级流水线），换来峰值吞吐减半——典型的「时间换面积」。

#### 4.2.3 源码精读

**独立加密**顶层文件用 4 个 `#include` 把零件拼起来（`chacha20` / `prep_auth_data` / `poly1305_mac` / `append_auth_tag`），每个零件都带一个实例前缀：

> [src/chacha20poly1305_encrypt.c:14-23](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_encrypt.c#L14-L23) —— 每个块用 `#define XXX_INST` 起一个实例名再 `#include` 对应 `.c`。

```c
#define CHACHA_INST chacha20_encrypt
#include "chacha20/chacha20.c"
#define PREP_AUTH_DATA_INST prep_auth_data_encrypt
#include "prep_auth_data/prep_auth_data.c"
#define POLY_MAC_INST poly1305_mac_encrypt
#include "poly1305/poly1305_mac.c"
#include "auth_tag/append_auth_tag.c"
```

**独立解密**顶层结构对称，但多 include 了三个解密专属块（`strip_auth_tag` / `poly1305_verify_decrypt` / `wait_to_verify`）：

> [src/chacha20poly1305_decrypt.c:13-27](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_decrypt.c#L13-L27) —— 解密把实例前缀换成 `*_decrypt`，并多挂三个验证相关块。

```c
#define CHACHA_INST chacha20_decrypt
#include "chacha20/chacha20.c"
// ... prep_auth_data / poly1305_mac 同样带 _decrypt 前缀 ...
#include "poly1305/poly1305_verify_decrypt.c"
#include "auth_tag/strip_auth_tag.c"
#include "auth_tag/wait_to_verify.c"
```

**共享变体**顶层则只 instantiate 一次共享流水线，并分别 include 加密、解密的具体连线：

> [src/chacha20poly1305_encrypt_decrypt_shared.c:7-19](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_encrypt_decrypt_shared.c#L7-L19) —— 共享一条 ChaCha20 流水线；注意 Poly1305 的共享被注释掉了。

```c
#pragma PART "xc7a200tffg1156-2" // Artix 7 200T
// 两条大计算流水线中的 ChaCha20 共享
#include "chacha20/chacha20_pipeline_shared.c"
// Poly1305 其实没那么大，不如不共享、靠 MCP 换吞吐
//#include "poly1305/poly1305_pipeline_shared.c"
#include "chacha20poly1305/encrypt_shared.c"
#include "chacha20poly1305/decrypt_shared.c"
```

这里有一处很值得品味的设计判断：**ChaCha20 流水线被共享，Poly1305 流水线却没有**。源码注释直言 Poly1305「没那么大（not that big）」，与其共享换来复杂度与吞吐减半，不如各自独占、靠 MCP（Multi-Cycle Pipeline，多周期流水线）拿回吞吐。这说明「共享」不是越多越好，要看具体模块的面积/延迟比——这正是工程师在面积与性能之间做的精细权衡。

#### 4.2.4 代码实践

**实践目标**：对比三个构建脚本的命令行差异，理解「同一工具、三个产物」。

**操作步骤**：

1. 并排打开 `build_verilog.sh`、`build_verilog_decrypt.sh`、`build_verilog_shared.sh`。
2. 逐字比较三者的 `--top` 名字、输入 `.c` 文件、`--out_dir` 目录。
3. （可选）分别运行三条脚本，或直接查看仓库里预生成的 `generated-files/chacha20poly1305_{encrypt,decrypt,encrypt_decrypt_shared}.v`。
4. 在三个产物顶层模块里数一数：共享变体里有几条 ChaCha20 流水线实例？独立加密里有几条？

**需要观察的现象**：

- 三个脚本的命令行结构完全一致，只有「源文件 / 顶层名 / 输出目录」三处不同。
- 共享变体的 ChaCha20 流水线实例数应明显少于「独立加密 + 独立解密」之和。

**预期结果**：

| 脚本 | `--top` | 输入源 | 输出目录 |
|---|---|---|---|
| `build_verilog.sh` | `chacha20poly1305_encrypt` | `..._encrypt.c` | `generated-files-verilog/` |
| `build_verilog_decrypt.sh` | `chacha20poly1305_decrypt` | `..._decrypt.c` | `generated-files-verilog-decrypt/` |
| `build_verilog_shared.sh` | `chacha20poly1305_encrypt_decrypt_shared` | `..._shared.c` | `generated-files-verilog-shared/` |

共享变体里 ChaCha20 流水线只例化 1 条（供加解密复用），而把独立加密和独立解密两份合起来看则是 2 条——这就是「省面积」的直观体现。（流水线实例数以实际产物为准，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么解密变体比加密变体多出 `strip_auth_tag`、`poly1305_verify_decrypt`、`wait_to_verify` 三个块，而加密没有？

**答案**：因为 AEAD 是「加密—后—认证」：加密端算出 tag 直接追加到密文后面即可；解密端收到的密文末尾带 tag，必须先剥离 tag（`strip_auth_tag`）、本地重算 tag 并与收到的 tag 比对（`poly1305_verify_decrypt`），在比对结果出来前把已解密的明文暂存（`wait_to_verify` FIFO），通过才放行——这是防篡改/防填充预言攻击的安全要求。

**练习 2**：既然共享能省面积，为什么 Poly1305 流水线的共享反被注释掉？

**答案**：Poly1305 流水线相对轻量，共享它省下的面积有限，却要引入 round-robin 复用与 ID 解复用的复杂度、并让加解密吞吐互相挤占。工程师判断「不如各自独占、用 MCP 提升吞吐」更划算。共享与否取决于模块的面积/延迟权衡，不是一刀切。

---

### 4.3 pragma 与实例化约定

#### 4.3.1 概念说明

要让 PipelineC 正确把 C 变成硬件，必须遵守一套「约定」：用 pragma 告诉它时钟频率、目标器件、哪个函数是顶层、哪里有组合环路；用宏约定顶层端口怎么展平成 Verilog 线；用 `#define`+`#include` 约定如何让同一个模块被例化多次且端口名互不冲突。本模块逐个讲清这些约定。

#### 4.3.2 核心流程

PipelineC 识别的关键 pragma 与宏：

| 约定 | 作用 | 本项目用在何处 |
|---|---|---|
| `#pragma PART "<器件>"` | 绑定目标 FPGA 型号 | `xc7a200tffg1156-2`（Artix-7 200T） |
| `#pragma MAIN_MHZ <函数> <频率>` | 标记顶层函数 + 设定时钟频率 | `encrypt_dataflow 80.0` → 生成 `clk_80p0` |
| `#pragma MAIN <函数>` | 标记一个函数为独立硬件模块 | IO 端口转换函数 |
| `#pragma FUNC_WIRES <函数>` | 该函数纯连线、无寄存器 | IO 端口转换函数 |
| `#pragma FEEDBACK <信号>` | 声明组合环路（反馈线） | 位宽转换的 ready 信号 |
| `DECL_INPUT` / `DECL_OUTPUT` | 声明展平的顶层 Verilog 端口 | AXIS 的 tdata/tkeep/tlast/tvalid/tready |
| `SIMULATION` 宏 | 切换「硬件端口」与「仿真驱动线」 | testbench `#define SIMULATION` |
| `MAIN(<前缀>)` / `PPCAT` | 模块实例化与端口名拼接 | `#define CHACHA_INST` 模式 |
| `GLOBAL_VALID_READY_PIPELINE_INST` | 例化一条 valid/ready 握手流水线 | ChaCha20 64 级流水线 |

两条核心思路：

1. **时钟来自 `MAIN_MHZ`**：它既标记顶层，又设定频率，生成的 Verilog 顶层会多出一个名为 `clk_<频率>p0` 的时钟端口（80 MHz → `clk_80p0`）。
2. **顶层端口「展平」**：Verilog 不支持 C 的结构体/数组当端口，所以用 `DECL_INPUT/OUTPUT` 把 `stream(axis128_t)` 拆成一根根标量线（`tdata`/`tkeep`/`tlast`/`tvalid`/`tready`）。

#### 4.3.3 源码精读

**① MAIN_MHZ —— 顶层与时钟**

> [src/chacha20poly1305/encrypt_dataflow.c:7-8](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_dataflow.c#L7-L8) —— `MAIN_MHZ` 把 `encrypt_dataflow` 标为顶层并设定 80 MHz。

```c
#pragma MAIN_MHZ encrypt_dataflow 80.0
void encrypt_dataflow(){
    ...
}
```

这个函数体内并不做计算，只是**用赋值把各个子模块的全局端口连起来**（例如把 chacha20 的输出 fork 给 prep_auth_data 和 append_auth_tag）。这正是 PipelineC 的连线风格：模块间靠「全局可见的线」+ 顶层函数里的赋值来接线，而非 Verilog 的 `wire ... assign` 或实例端口映射。

**② DECL_INPUT/OUTPUT 与 SIMULATION 守卫**

> [src/chacha20poly1305/chacha20poly1305_encrypt.h:12-33](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/chacha20poly1305_encrypt.h#L12-L33) —— `#ifndef SIMULATION` 内用 `DECL_INPUT/OUTPUT` 声明展平端口。

```c
#ifndef SIMULATION
DECL_INPUT(key_uint_t, encrypt_key)
...
DECL_INPUT(uint128_t, encrypt_s_axis_tdata)
DECL_INPUT(uint16_t, encrypt_s_axis_tkeep)
DECL_INPUT(uint1_t,   encrypt_s_axis_tlast)
DECL_INPUT(uint1_t,   encrypt_s_axis_tvalid)
DECL_OUTPUT(uint1_t,  encrypt_s_axis_tready)
DECL_OUTPUT(uint128_t, encrypt_m_axis_tdata)
...
#endif
```

注意命名遵循 AXIS 的 **manager/subordinate** 约定：`s_axis_*`（subordinate，输入流）、`m_axis_*`（manager，输出流）。这些 `DECL_*` 只在**非仿真**时生效；仿真时 testbench 先 `#define SIMULATION` 再 include 设计，于是这些硬件端口被跳过，换成由 testbench 驱动的线：

> [src/chacha20poly1305_encrypt_tb.c:6-7](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_encrypt_tb.c#L6-L7) —— 测试台定义 `SIMULATION` 后再 include 设计，使端口声明切换为仿真模式。

```c
#define SIMULATION
#include "chacha20poly1305_encrypt.c"
#include "chacha20poly1305/encrypt_tb.c"
```

`#ifndef SIMULATION` 的另一半（[chacha20poly1305_encrypt.h:48-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/chacha20poly1305_encrypt.h#L48-L69)）里有一个 `chacha20poly1305_encrypt_io_wires` 函数，负责把展平的标量端口与结构体线互相转换（`UINT_TO_BYTE_ARRAY` 等），让一份代码既能综合成硬件、又能跑仿真。

**③ `#define` 多实例化模式**

这是 PipelineC 项目里最值得学的设计模式。看 chacha20.c：

> [src/chacha20/chacha20.c:14-16](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.c#L14-L16) 与 [src/chacha20/chacha20.c:20-30](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.c#L20-L30) —— 用 `CHACHA_INST` 宏 + `PPCAT` 拼出带前缀的端口名。

```c
#ifndef CHACHA_INST
#define CHACHA_INST chacha20      // 默认前缀
#endif
// 带前缀的全局端口
uint8_t PPCAT(CHACHA_INST,_key)[CHACHA20_KEY_SIZE];        // → chacha20_encrypt_key
stream(axis128_t) PPCAT(CHACHA_INST,_axis_in);             // → chacha20_encrypt_axis_in
...
MAIN(CHACHA_INST)                                          // 顶层模块名也带前缀
void CHACHA_INST(){ ... }
#undef CHACHA_INST                                          // 用完取消，便于再次 include
```

`PPCAT(A,B)` 是「预处理拼接」，把 `chacha20_encrypt` 和 `_key` 拼成 `chacha20_encrypt_key`。于是同一份 `chacha20.c` 被 include 两次——一次 `#define CHACHA_INST chacha20_encrypt`（加密侧），一次 `#define CHACHA_INST chacha20_decrypt`（解密侧）——就能得到两组**端口名互不冲突**的实例。末尾的 `#undef`（[chacha20.c:56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.c#L56)）确保下一次 include 能换新前缀。这本质上是 C 预处理器模拟出的「模块例化」。

**④ FEEDBACK 与 axis128↔axis512 位宽转换**

ChaCha20 运算核吃 512 位（64 字节）块，但总线只有 128 位，因此进出核都要做位宽转换。这一转换发生在 `chacha20_fsm` 内：

> [src/chacha20/chacha20.h:294-297](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.h#L294-L297) —— 输入侧：`axis128_to_axis512` 把 4 个 128 位 beat 拼成 1 个 512 位块，`FEEDBACK` 声明 ready 反馈线。

```c
uint1_t block_in_ready;
#pragma FEEDBACK block_in_ready
axis128_to_axis512_t in_to_block = axis128_to_axis512(dwidth_conv_data_in, block_in_ready);
stream(axis512_t) block_in_stream = in_to_block.axis_out;
```

> [src/chacha20/chacha20.h:342-373](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.h#L342-L373) —— 输出侧：`axis512_to_axis128` 把 512 位块拆回 4 个 128 位 beat，ready 信号同样用 `FEEDBACK` 闭环。

```c
#pragma FEEDBACK block_to_out_axis_in_ready
...
axis512_to_axis128_t block_to_out = axis512_to_axis128(block_to_out_axis_in, ready_for_axis_out);
block_to_out_axis_in_ready = block_to_out.axis_in_ready; // FEEDBACK
```

为什么需要 `#pragma FEEDBACK`？因为位宽转换器的 `ready` 输出取决于下游当前是否吃数据，而下游是否吃数据又反过来依赖这个 ready——形成**组合环路**。普通 PipelineC 函数默认无组合环路，必须用 `FEEDBACK` 显式声明这根「回环线」，工具才会保留这条组合反馈路径而不报错。

而那条昂贵的 64 级流水线本身，用 `GLOBAL_VALID_READY_PIPELINE_INST` 一行例化（[chacha20.c:34](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.c#L34)），最后一个参数 `64` 就是流水线深度：

```c
GLOBAL_VALID_READY_PIPELINE_INST(PPCAT(CHACHA_INST,_pipeline), axis512_t, chacha20_loop_body, chacha20_loop_body_in_t, 64)
```

#### 4.3.4 代码实践

**实践目标**：在生成的 Verilog 里定位「axis128↔axis512 位宽转换」与顶层端口，把 C 约定对应到 Verilog 产物。

**操作步骤**：

1. 打开 `generated-files/chacha20poly1305_encrypt.v`（运行 `build_verilog.sh` 后看 `generated-files-verilog/` 下同名文件亦可）。
2. 搜索模块名 `axis128_to_axis512` 与 `axis512_to_axis128`，确认它们都被生成了。
3. 搜索 `module chacha20poly1305_encrypt(`，核对其端口列表与 `chacha20poly1305_encrypt.h` 里 `DECL_INPUT/OUTPUT` 声明一一对应。
4. 回到 C 源码 [chacha20.h:294-297](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.h#L294-L297)，确认位宽转换正好夹在「128 位总线」与「512 位 ChaCha20 流水线」之间。

**需要观察的现象**：

- Verilog 里存在 `axis128_to_axis512_*` 与 `axis512_to_axis128_*` 两个模块，分别负责「拼装」与「拆分」。
- 顶层模块端口是扁平标量（`encrypt_s_axis_tdata[127:0]` 等），没有结构体——印证了 `DECL_INPUT/OUTPUT` 的展平作用。

**预期结果**：位宽转换的位置如下——

```text
顶层 encrypt_s_axis_tdata (128 bit, AXIS 从机)
        │
        ▼  chacha20_fsm 内
   axis128_to_axis512   ← 每 4 拍拼 1 个 512 bit 块   [chacha20.h:296]
        │
        ▼
   ChaCha20 64 级流水线  (axis512)                    [chacha20.c:34]
        │
        ▼
   axis512_to_axis128   ← 每个块拆回 4 拍 128 bit       [chacha20.h:371]
        │
        ▼
顶层 encrypt_m_axis_tdata (128 bit, AXIS 主机)
```

位宽比为 512 / 128 = 4，即每 4 个 128 位 beat 对应 1 个 64 字节 ChaCha20 块。（具体模块实例名以实际产物为准，待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 `#pragma MAIN_MHZ encrypt_dataflow 80.0` 的 `80.0` 改成 `125.0`，生成的 Verilog 顶层会发生什么变化？能保证一定综合得过吗？

**答案**：顶层时钟端口名会从 `clk_80p0` 变成 `clk_125p0`，且 PipelineC 会按 125 MHz 目标去调度流水线。但这只是「目标频率」，能否综合得过取决于设计的关键路径延迟——Artix-7 普通逻辑常在 100 MHz 上下，盲目提到 125 MHz 很可能时序不满足（回顾 u1-l3 提到的器件频率上限）。`MAIN_MHZ` 给的是期望，不是保证。

**练习 2**：为什么 `DECL_INPUT/OUTPUT` 要包在 `#ifndef SIMULATION` 里？仿真时这些端口由谁驱动？

**答案**：综合成硬件时需要这些扁平端口与外部（FPGA 引脚/上层模块）相连；而仿真时不走真实端口，testbench 用驱动线直接喂数据。包在 `#ifndef SIMULATION` 里，使得同一份 `.h` 在仿真时跳过端口声明、改用 testbench 提供的等价线，从而一份设计源码两用。仿真时端口由 testbench 文件（如 `encrypt_tb.c`）驱动。

**练习 3**：`chacha20.c` 末尾为什么要有 `#undef CHACHA_INST`？

**答案**：因为同一份 `chacha20.c` 会被 include 多次（加密侧、解密侧各一次），每次用不同的 `CHACHA_INST` 前缀生成不同实例。若不 `#undef`，第二次 include 时旧前缀还在，会导致端口名拼错或重复定义。`#undef` 让每次 include 都从「干净」状态开始，这是用 C 预处理器模拟多实例化的必要收尾。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从 C 到 Verilog 的完整追踪」：

1. **选变体**：打开 `src/chacha20poly1305_encrypt.c`（独立加密）。指出它由哪几个零件 include 而成，每个零件的实例前缀是什么。
2. **找约定**：在这份文件及其 include 链里，找出本讲讲过的全部约定各出现一次的位置——`PART`、`MAIN_MHZ`、`DECL_INPUT/OUTPUT`、`SIMULATION` 守卫、`#define INST` + `#include`、`FEEDBACK`、`GLOBAL_VALID_READY_PIPELINE_INST`。
3. **看产物**：运行 `build_verilog.sh`（或读 `generated-files/chacha20poly1305_encrypt.v`），找到顶层模块 `chacha20poly1305_encrypt`，把它的每个端口回填到 `chacha20poly1305_encrypt.h` 里对应的 `DECL_INPUT/OUTPUT` 声明。
4. **画数据通路**：在生成的 Verilog 里定位 `axis128_to_axis512` 与 `axis512_to_axis128`，画出「128 位总线 → 位宽转换 → 64 级 ChaCha20 流水线 → 位宽转换 → 128 位总线」的通路，标注每段的位宽。
5. **思考延伸**：若要改成共享变体，你会把哪条流水线从「两条」改成「一条」，又需要引入什么机制保证加解密结果不串台？（提示：`is_encrypt` 标签穿越流水线。）

完成后，你应当能用一句话向同伴解释：「PipelineC 把这份 C，靠 pragma 标好时钟与器件，靠 `#define` 多实例化，编出了一份扁平端口的 Verilog，加密核就长这样。」

## 6. 本讲小结

- PipelineC 是一套开源 HLS，靠 `$PIPELINEC <src.c> --top <name> --verilog` 把 C 函数直接编译成可综合 Verilog，每次从零重建。
- 本项目有三条等结构的构建脚本，对应三种设计变体：独立加密、独立解密、加解密共享，差异在面积与吞吐的取舍。
- 共享变体让加解密复用一条 64 级 ChaCha20 流水线，靠 `is_encrypt` 标签穿越流水线做 round-robin 输入复用与 ID 解复用；但 Poly1305 因「没那么大」而选择不共享。
- `#pragma PART` 绑定器件、`MAIN_MHZ` 设顶层与时钟频率（80 MHz → `clk_80p0`）、`FEEDBACK` 声明组合环路、`DECL_INPUT/OUTPUT` 展平端口。
- `#define <INST>` + `#include` + `PPCAT` + `#undef` 是用 C 预处理器模拟「模块多实例化」的模式，让同一份 `.c` 生成端口名互不冲突的多个实例。
- ChaCha20 流水线吃 512 位块、总线是 128 位，进出核各有一次 axis128↔axis512 位宽转换（4:1），位于 `chacha20_fsm` 内、用 `FEEDBACK` 闭环 ready。

## 7. 下一步学习建议

本讲只讲了「怎么生成 Verilog」与「三套变体的骨架」，把每个功能块当黑盒。接下来：

- **u5-l3 加密数据流**：拆开 `encrypt_dataflow` 的连线，精读密文分叉、`prep_auth_data` 的 AAD/密文/长度 FSM、`append_auth_tag` 如何抑制再恢复 `tlast`。
- **u5-l4 解密与验证数据流**：精读 `strip_auth_tag` 的 early-tlast look-ahead、`poly1305_verify` 的 128 位原子比对、`wait_to_verify` 的 FIFO 缓存与「验证失败丢弃」安全机制。
- **u5-l5 资源共享**：深入 `chacha20_pipeline_shared` 的 round-robin 调度与 ID 解复用细节，理解背压在共享下如何处理。
- **u5-l6 Pypeline Python 前端**：看同一套三变体如何用 Python 重写，并修正 C 版 Poly1305 的数学 bug——可与本讲的 C 版逐行对照。
