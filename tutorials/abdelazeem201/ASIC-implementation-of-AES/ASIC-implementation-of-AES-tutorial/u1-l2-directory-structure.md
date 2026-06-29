# 目录结构与文件清单

## 1. 本讲目标

上一讲（u1-l1）我们已经从 README 和算法层面认识了本仓库：它是一个用 Verilog 写的、面向 FPGA/ASIC 的 AES 加解密核，工作在 ECB 单块模式，支持 128/256 位密钥。但「算法」和「代码仓库」不是一回事——要知道这些功能到底落在哪些文件里，就必须先读懂仓库的物理结构。

本讲学完后，你应当能够：

- 说出仓库里 `rtl/`、`Pre-Synthesis Simulation/`、`Project Pics/` 三个目录各自的作用，以及它们之间的对应关系。
- 区分「设计模块（可综合 RTL）」和「测试平台（testbench，仅用于仿真）」两类 Verilog 文件。
- 用一张表列出 `rtl/` 下全部 12 个 `.v` 文件，标注每个文件是设计模块还是测试平台，并写出一句话职责。
- 看懂本工程「顶层 wrapper → core → 多个子模块」的实例化层级关系。

> 说明：本讲的实践任务最初提到「13 个 `.v` 文件」，但实际仓库 `rtl/` 目录下共有 **12 个** `.v` 文件（7 个设计模块 + 5 个测试平台）。本讲始终以源码实际内容为准，下文表格据此填写。

## 2. 前置知识

- **RTL（Register Transfer Level，寄存器传输级）**：用 Verilog 描述的、最终能被综合成真实硬件电路（逻辑门、触发器）的代码。本仓库的设计文件都是 RTL。
- **Testbench（测试平台）**：一段「不会被综合成硬件」的 Verilog 代码，它的唯一作用是给设计模块喂激励（输入信号）、观察输出、判断对错。工程里约定俗成：测试平台文件名以 `tb_` 开头，例如 `tb_aes.v`。
- **实例化（instantiation）**：在一个模块里「调用」另一个模块，就像编程里调用子函数。本工程通过层层实例化把多个小模块组装成一个完整的 AES 核。
- **DUT（Design Under Test，被测设计）**：测试平台正在测试的那个设计模块。
- **可综合 / 不可综合**：`always @(posedge clk ...)` 这类寄存器逻辑、`assign` 这类组合逻辑可以综合；而 `#10` 延时、`$display` 打印、`initial` 里的激励序列只用于仿真，不可综合。这是区分设计文件和测试文件的根本依据。

> 这些概念在上一讲已铺垫过 AES 的算法背景（对称分组密码、ECB 单块、128/256 位密钥、S-box、轮密钥等术语），本讲直接使用，不再重复解释。

## 3. 本讲源码地图

本讲主要在「仓库层面」活动，重点是看清每个文件扮演什么角色。涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目说明，给出定位、256 位密钥、0.06 Gbps 吞吐等高层信息 |
| `rtl/` | **设计源码与测试平台的真正归宿**，共 12 个 `.v` 文件 |
| `Pre-Synthesis Simulation/` | ModelSim 仿真工程目录，存放与 `rtl/` 相同的 12 个 `.v` 副本，外加 `.mpf` 等工程文件 |
| `Project Pics/` | 文档配图（`AES.jpg`、`Pre-Synthesis_Simulation.PNG`） |
| `rtl/aes.v` | 顶层 wrapper：把 AES 核包成一个带总线接口的可访问模块 |
| `rtl/aes_core.v` | AES 核心：实例化各子模块、运行控制状态机 |
| `rtl/tb_aes.v` | 顶层 testbench：对 `aes` wrapper 做端到端自检测试 |

## 4. 核心概念与源码讲解

### 4.1 目录结构

#### 4.1.1 概念说明

一个 Verilog 工程通常不只放代码，还会放仿真工程、文档配图、说明文件。本仓库的根目录（commit `585f265`）结构非常简洁，只有四个条目加一个 README：

```
ASIC-implementation-of-AES/
├── README.md                       # 项目说明（上一讲已精读）
├── rtl/                            # ★ 设计源码 + 测试平台（共 12 个 .v）
├── Pre-Synthesis Simulation/       # ModelSim 仿真工程（与 rtl/ 同源 .v + 工程文件）
└── Project Pics/                   # 文档配图
```

理解目录结构的关键，是分清两类「看起来一样」的东西：

1. **`rtl/` 是「源」**：所有 `.v` 文件的权威版本都放在这里。改代码、读代码，都应该面向 `rtl/`。
2. **`Pre-Synthesis Simulation/` 是「仿真工程副本」**：它里面的 `.v` 文件和 `rtl/` 里的一模一样（甚至字节数都相同），但额外携带了 ModelSim 的工程产物。

#### 4.1.2 核心流程

判断「一个目录到底是什么」可以套用下面这个简单决策流程：

```text
看到 .v 文件
   │
   ├── 文件名以 tb_ 开头？ ── 是 ──▶ 测试平台（testbench），不可综合
   │
   └── 否 ──▶ 设计模块（design），可综合 RTL
```

判断「这个目录是源码还是仿真工程」：

```text
目录里只有 .v？
   ├── 是 ──▶ 源码目录（rtl/）
   └── 否（还有 .mpf / .wlf / work/ 等） ──▶ 仿真工程目录（Pre-Synthesis Simulation/）
```

把上面两条规则组合，本仓库的三个目录职责就一目了然：

| 目录 | 内容 | 角色 |
|------|------|------|
| `rtl/` | 仅 12 个 `.v` | 源码：设计 + 测试平台都放这里 |
| `Pre-Synthesis Simulation/` | 同样的 12 个 `.v` + `simulation.mpf`、`simulation.cr.mti`、`vsim.wlf`、`@_opt/`、`work/` | ModelSim 仿真工程，可直接打开跑仿真 |
| `Project Pics/` | `AES.jpg`、`Pre-Synthesis_Simulation.PNG` | README 引用的配图 |

> 小知识：`.mpf` 是 ModelSim 的工程文件（Modelsim Project File），`.wlf` 是波形日志（Wave Log Format），`work/` 与 `@_opt/` 是编译产物库。它们都是「仿真专用、不是源码」，所以不要去读它们的文本内容，更不要把它们当成 Verilog 来改。后续 u1-l5「运行仿真」会专门讲怎么用这个工程。

#### 4.1.3 源码精读

为了印证「`rtl/` 与 `Pre-Synthesis Simulation/` 里的是同一批 `.v`」，我们可以直接看文件本身。先看 `rtl/aes.v` 的开头，确认这是一份正常的、带注释的 Verilog 源文件：

[rtl/aes.v:L1-L8](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L1-L8) —— 文件头注释，说明本文件是「AES 核的顶层 wrapper」。

[rtl/aes.v:L9-L22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22) —— `module aes(...)` 的端口列表，给出 `clk / reset_n / cs / we / address / write_data / read_data`，这就是本核对外的总线接口（u1-l4 会细讲）。

再看 `Pre-Synthesis Simulation/aes.v`，其文件头与端口列表与 `rtl/aes.v` 完全一致（字节数同为 7291），印证了「仿真工程里的 `.v` 只是 `rtl/` 的副本」这一结论。

而 README 本身在根目录，并且引用了 `Project Pics/` 下的图片来配图：

[README.md:L4-L7](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/README.md#L4-L7) —— README 用 `<img src= "...Project%20Pics/AES.jpg">` 等标签引用 `Project Pics/` 目录下的配图。

#### 4.1.4 代码实践

**实践目标**：亲手确认仓库的目录结构与「两份 `.v` 是否相同」。

**操作步骤**：

1. 在仓库根目录列出顶层条目。
2. 列出 `rtl/` 与 `Pre-Synthesis Simulation/` 下的文件。
3. 用逐字节比对，验证两个目录里的同名 `.v` 是否完全一致。

**示例命令**（Linux/macOS 终端，注意仿真目录名含空格需加引号）：

```bash
# 1) 顶层目录
ls -la

# 2) 列出两个目录里的 .v 文件
ls rtl/
ls "Pre-Synthesis Simulation/"

# 3) 逐个比对同名文件是否一致（diff 无输出即表示完全相同）
for f in $(ls rtl/); do
  diff -q "rtl/$f" "Pre-Synthesis Simulation/$f"
done
```

**需要观察的现象**：

- `rtl/` 下只有 12 个 `.v` 文件，没有任何工程产物。
- `Pre-Synthesis Simulation/` 下除了同样的 12 个 `.v`，还有 `simulation.mpf`、`vsim.wlf`、`work/`、`@_opt/` 等。
- 第三步的 `diff` 对每个文件都「无输出」，说明两份 `.v` 完全一致。

**预期结果**：两个目录里的 12 个 `.v` 文件两两相同；区别只在于 `Pre-Synthesis Simulation/` 多了 ModelSim 工程产物。结论——**读源码看 `rtl/`，跑仿真用 `Pre-Synthesis Simulation/`**。

> 待本地验证：若你在 Windows 下没有 `diff`，可用 `fc rtl\aes.v "Pre-Synthesis Simulation\aes.v"`（ModelSim 自带或系统 `fc`）达到同样目的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Pre-Synthesis Simulation/` 里的 `.v` 文件和 `rtl/` 里的一模一样，工程还要保留两份？

**参考答案**：`Pre-Synthesis Simulation/` 是一个 ModelSim 工程目录，工程文件（`.mpf`）里记录的源码路径指向它自己目录下的副本；这样工程可以「自包含」地复制到别处直接打开。`rtl/` 才是源码权威版本。改代码应改 `rtl/`（并在仿真工程里同步），不要去改仿真目录里的副本。

**练习 2**：`Project Pics/` 目录会被综合工具（如 Synopsys/Quartus）处理吗？它对 AES 核的硬件实现有影响吗？

**参考答案**：不会，也没有影响。综合工具只读取 `.v` 等 HDL 文件；`Project Pics/` 里只有 `.jpg`/`.PNG` 图片，是文档配图，仅供 README 展示用，与电路无关。

---

### 4.2 设计文件清单

#### 4.2.1 概念说明

本工程 `rtl/` 下的 12 个 `.v` 文件可分为两大类：

- **设计模块（7 个）**：文件名**不以** `tb_` 开头，描述真实硬件电路，最终会被综合成芯片上的逻辑。例如 `aes.v`、`aes_core.v`、`aes_sbox.v`。
- **测试平台（5 个）**：文件名**以** `tb_` 开头，仅用于仿真，不进芯片。例如 `tb_aes.v`。

这 7 个设计模块不是平铺的，而是通过实例化组成一棵层级树：最顶层是 `aes`，它把整个 AES 核包成一个可被总线访问的模块；`aes_core` 是真正的核心，它实例化了加密、解密、S-box、密钥存储四个子模块。

#### 4.2.2 核心流程

设计模块的实例化层级如下（`→` 表示「实例化了」）：

```text
aes  (rtl/aes.v，顶层 wrapper，提供总线接口)
 └─→ aes_core  (rtl/aes_core.v，核心：状态机 + 多路选择)
       ├─→ aes_encipher_block   enc_block   (加密数据通路 + 加密轮 FSM)
       ├─→ aes_decipher_block   dec_block   (解密数据通路 + 解密轮 FSM)
       │      └─→ aes_inv_sbox  inv_sbox_inst  (解密专用的逆 S-box)
       ├─→ aes_key_mem          keymem      (密钥扩展 + 轮密钥存储)
       └─→ aes_sbox             sbox_inst   (正向 S-box，被加密与密钥扩展共享)
```

几个要点（后续讲义会逐个展开）：

- **只有一个共享 S-box**：`aes_core` 只实例化了一个 `aes_sbox`（`sbox_inst`），加密通路（`enc_block`）和密钥扩展（`keymem`）通过一个多路选择器轮流使用它——这是本工程「用时间换面积」的关键设计（u3-l4 会专题讨论）。
- **解密自带逆 S-box**：`aes_decipher_block` 内部实例化了自己的 `aes_inv_sbox`（`inv_sbox_inst`），所以解密用的是「逆查表」。
- **除 `aes` 外的设计模块都各有独立 testbench**，但 `aes_sbox` / `aes_inv_sbox` 没有单独的 testbench（它们被包含在 encipher/decipher 的测试里）。

#### 4.2.3 源码精读

逐个看 7 个设计模块的「身份证」（文件头注释 + module 声明）：

[rtl/aes.v:L9-L22](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L9-L22) —— 顶层 wrapper `module aes(...)`，对外暴露总线端口。

[rtl/aes_core.v:L2-L7](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L2-L7) —— `aes_core` 的注释：「支持 128 与 256 位密钥，大部分功能在子模块里」。注意这与 README「只说 256 位」不同，源码才是权威（上一讲已指出）。

[rtl/aes_core.v:L10-L25](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L10-L25) —— `module aes_core(...)` 的端口，包含 `encdec / init / next / key / keylen / block / result` 等。

最能说明「层级关系」的是 `aes_core.v` 的实例化段——一段代码同时体现了「设计模块清单」和「谁调用谁」：

[rtl/aes_core.v:L83-L138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L83-L138) —— `aes_core` 内部依次实例化了：
- [rtl/aes_core.v:L86-L102](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L86-L102)：加密模块 `aes_encipher_block enc_block(...)`；
- [rtl/aes_core.v:L105-L118](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L105-L118)：解密模块 `aes_decipher_block dec_block(...)`；
- [rtl/aes_core.v:L121-L135](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L121-L135)：密钥存储 `aes_key_mem keymem(...)`；
- [rtl/aes_core.v:L138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L138)：共享 S-box `aes_sbox sbox_inst(...)`。

而顶层 wrapper 对 core 的实例化在 `aes.v` 里：

[rtl/aes.v:L116-L130](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L116-L130) —— `aes_core core(...)`，把 wrapper 的内部信号接到 core 上。

其余设计模块的 `module` 声明行号（备查）：

- [rtl/aes_sbox.v:L10](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_sbox.v#L10)：`module aes_sbox(...)`，注释说是「256 字节 ROM，4 路并行处理 32 位字」。
- [rtl/aes_inv_sbox.v:L9](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_inv_sbox.v#L9)：`module aes_inv_sbox(...)`，逆向 S-box。
- [rtl/aes_key_mem.v:L9](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_key_mem.v#L9)：`module aes_key_mem(...)`，密钥存储含轮密钥生成器。
- [rtl/aes_encipher_block.v:L11](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_encipher_block.v#L11)：`module aes_encipher_block(...)`，加密轮（组合 + 状态机）。
- [rtl/aes_decipher_block.v:L10](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L10)：`module aes_decipher_block(...)`，解密轮（组合 + 状态机）。
- [rtl/aes_decipher_block.v:L205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)：解密模块内部实例化逆 S-box `aes_inv_sbox inv_sbox_inst(...)`。

#### 4.2.4 代码实践

**实践目标**：把 7 个设计模块整理成一张「清单表」，并用源码验证它们的层级关系。

**操作步骤**：

1. 打开 `rtl/` 目录，挑出所有**不以** `tb_` 开头的 `.v` 文件。
2. 阅读每个文件开头 10 行的注释，提炼一句话职责。
3. 对照 4.2.3 中的实例化代码，确认每个模块的「父模块」是谁。

**参考答案表**（设计模块部分）：

| # | 文件 | 类型 | 一句话职责 |
|---|------|------|-----------|
| 1 | `aes.v` | 设计模块 | 顶层 wrapper，把 AES 核包成带总线接口（`cs/we/address/...`）的可访问模块 |
| 2 | `aes_core.v` | 设计模块 | AES 核心，运行控制状态机，实例化并连接各子模块 |
| 3 | `aes_encipher_block.v` | 设计模块 | 加密数据通路：SubBytes/ShiftRows/MixColumns/AddRoundKey 与加密轮状态机 |
| 4 | `aes_decipher_block.v` | 设计模块 | 解密数据通路：逆变换与解密轮状态机，内部含逆 S-box |
| 5 | `aes_sbox.v` | 设计模块 | 正向 S-box，256 字节 ROM 查表，4 路并行处理 32 位字 |
| 6 | `aes_inv_sbox.v` | 设计模块 | 逆向 S-box，256 字节 ROM 查表，供解密使用 |
| 7 | `aes_key_mem.v` | 设计模块 | 密钥存储 + 轮密钥生成器（密钥扩展） |

**需要观察的现象**：7 个设计模块中，`aes_sbox` 与 `aes_inv_sbox` **没有**对应的独立 testbench，而另外 4 个功能模块（`aes`、`aes_core`、`aes_encipher_block`、`aes_decipher_block`）以及 `aes_key_mem` 都有（见下一节）。

**预期结果**：你能凭这张表，指着任何一个设计模块说出「它由谁实例化、它实例化了谁」。例如 `aes_encipher_block` 由 `aes_core` 实例化为 `enc_block`，自身不再实例化别的模块。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `aes_core` 只实例化了一个 `aes_sbox`，而 `aes_encipher_block` 和 `aes_key_mem` 都要用 S-box？

**参考答案**：加密的 SubBytes 和密钥扩展里的字变换都需要正向 S-box。本工程为了省面积（ROM 资源），只放一个 `aes_sbox`，用一个多路选择器让加密通路和密钥扩展轮流使用它。代价是 SubBytes 要拆成多拍、用更长的时间换取更小的面积。

**练习 2**：`aes_inv_sbox` 是被谁实例化的？为什么它没有像 `aes_sbox` 那样放在 `aes_core` 里共享？

**参考答案**：`aes_inv_sbox` 被 `aes_decipher_block`（在 [rtl/aes_decipher_block.v:L205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)）实例化为 `inv_sbox_inst`。逆向 S-box 只有解密用得到，密钥扩展用的是正向 S-box，所以无需共享，自然放在解密模块内部。

---

### 4.3 测试文件清单

#### 4.3.1 概念说明

5 个测试平台文件都以 `tb_` 开头，命名规律是 `tb_<被测模块名>.v`，几乎一一对应一个设计模块：

| 测试平台 | 被测设计（DUT） |
|----------|----------------|
| `tb_aes.v` | `aes`（顶层 wrapper） |
| `tb_aes_core.v` | `aes_core` |
| `tb_aes_encipher_block.v` | `aes_encipher_block` |
| `tb_aes_decipher_block.v` | `aes_decipher_block` |
| `tb_aes_key_mem.v` | `aes_key_mem` |

这种「一个模块配一个独立 testbench」的写法，叫做**分层测试**（hierarchical testing）：底层模块先各自验证好，再逐层向上组合验证，最后在顶层 `tb_aes` 跑端到端的 NIST 已知应答测试。u3-l2、u3-l3 会专门讲。

> 注意：`aes_sbox` / `aes_inv_sbox` 没有独立 testbench——它们的正确性被「裹」在 encipher/decipher 的测试里间接验证。

#### 4.3.2 核心流程

一个典型 testbench 的内部结构（以 `tb_aes.v` 为例）：

```text
module tb_aes();          # 无端口——testbench 不对外，自己产生一切
  reg  clk, reset_n;      # 1. 信号声明
  ...                     #    实例化 DUT：dut(.clk(clk), ...)
  initial begin           # 2. 激励：产生时钟、复位、按地址写命令/读结果
    ...init_key(...);     # 3. 调用任务（task）做自检
    ...ecb_mode_single_block_test(...);
    $display(...);        # 4. 打印结果，统计 error_ctr
    $finish;
  end
```

testbench 的「流程」其实就是：**造时钟与复位 → 调用任务驱动 DUT → 比对期望值 → 打印通过/失败**。其中「任务（task）」是 Verilog 里类似函数的封装，例如 `init_key`、`ecb_mode_single_block_test`、`aes_test`（u3-l2 详解）。

#### 4.3.3 源码精读

各 testbench 的 `module` 声明（注意它们都是无端口的 `tb_xxx()`）：

- [rtl/tb_aes.v:L3-L14](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L3-L14) —— 注释「Testbench for the aes top level wrapper」与 `module tb_aes();`。
- [rtl/tb_aes_core.v:L3-L12](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L3-L12) —— `module tb_aes_core();`，测试 AES 核。
- [rtl/tb_aes_encipher_block.v:L3-L12](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L3-L12) —— `module tb_aes_encipher_block();`，单独测加密模块。
- [rtl/tb_aes_decipher_block.v:L4-L13](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_decipher_block.v#L4-L13) —— `module tb_aes_decipher_block();`，单独测解密模块。
- [rtl/tb_aes_key_mem.v:L3-L12](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L3-L12) —— `module tb_aes_key_mem();`，单独测密钥存储/扩展。

在 `tb_aes.v` 内部可以看到任务驱动 DUT 的痕迹，例如它会把整组 NIST 测试组织成 `aes_test` 任务（这是 u3-l2 的核心，这里只需知道它存在）：

[rtl/tb_aes_core.v:L312](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L312) —— `aes_core_test` 任务注释，说明 core 的测试也采用了类似的「任务封装 + 自检」结构。

#### 4.3.4 代码实践

**实践目标**：把 5 个测试平台整理成清单表，并为每个 tb 指明它的 DUT。

**操作步骤**：

1. 在 `rtl/` 中挑出所有**以** `tb_` 开头的 `.v` 文件。
2. 在每个 testbench 里搜索它实例化的设计模块（即 DUT）。
3. 填写下表。

**参考答案表**（测试平台部分）：

| # | 文件 | 类型 | 被测设计（DUT） | 一句话职责 |
|---|------|------|----------------|-----------|
| 8  | `tb_aes.v` | 测试平台 | `aes` | 顶层端到端自检：跑 NIST ECB 已知应答（AES-128/256）并统计错误数 |
| 9  | `tb_aes_core.v` | 测试平台 | `aes_core` | 单独测核心：密钥扩展 + 加解密的组合行为 |
| 10 | `tb_aes_encipher_block.v` | 测试平台 | `aes_encipher_block` | 单独测加密模块一轮一轮的正确性 |
| 11 | `tb_aes_decipher_block.v` | 测试平台 | `aes_decipher_block` | 单独测解密模块一轮一轮的正确性 |
| 12 | `tb_aes_key_mem.v` | 测试平台 | `aes_key_mem` | 单独测密钥扩展与轮密钥存储 |

**需要观察的现象**：每个 testbench 都是无端口的 `module tb_xxx();`，并且内部都有一处实例化了对应的 DUT。文件大小上，`tb_aes_key_mem.v`（约 24KB）最大，因为它要枚举 128/256 位两种密钥扩展的大量中间轮密钥。

**预期结果**：你能说出「要验证某个设计模块，应运行哪个 testbench」。例如验证密钥扩展用 `tb_aes_key_mem`，验证整个核用 `tb_aes`。具体怎么编译运行，见 u1-l5。

> 待本地验证：各 testbench 实例化 DUT 的那一行（如 `aes dut(.clk(clk), ...)`）的准确行号，建议你用编辑器在文件里搜索 `aes` / `aes_core` / `aes_encipher_block` / `aes_decipher_block` / `aes_key_mem` 自行确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `aes_sbox` 没有独立的 `tb_aes_sbox.v`？它的正确性如何被验证？

**参考答案**：S-box 的查表正确性会在 encipher / decipher / key_mem 各自的 testbench 里被间接覆盖——只要这些上层模块的测试通过，S-box 的查表就一定是对的。所以工程没有为它单独写 testbench，避免重复劳动。

**练习 2**：如果要给 `aes_inv_sbox` 补一个独立的 `tb_aes_inv_sbox.v`，应该参照哪个现有 testbench 的写法？

**参考答案**：可以参照 `tb_aes_encipher_block.v` 或 `tb_aes_decipher_block.v` 的骨架：声明无端口的 `module tb_aes_inv_sbox();`，实例化 `aes_inv_sbox` 作为 DUT，然后在 `initial` 块里给定若干 `sword` 输入、比对 `new_sword` 输出（正/逆 S-box 是互逆的，可用已知 S-box 表反推期望值）。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成一张「`rtl/` 全量文件清单表」——这也就是本讲核心的实践任务。

**任务**：

1. 用一张表列出 `rtl/` 下**全部 12 个** `.v` 文件（注意：实际是 12 个，不是 13 个）。
2. 每行标注：文件名、类型（设计模块 / 测试平台）、它实例化的或被它测试的模块、一句话职责。
3. 在表后画一张「实例化层级树」，把 7 个设计模块的父子关系连起来，并标出 5 个 testbench 各自挂在哪个设计模块上。

**参考产出（合并表）**：

| # | 文件 | 类型 | 关联模块 / DUT | 一句话职责 |
|---|------|------|----------------|-----------|
| 1 | `aes.v` | 设计 | 实例化 `aes_core` | 顶层 wrapper，提供总线接口 |
| 2 | `aes_core.v` | 设计 | 实例化 enc/dec/key_mem/sbox | 核心控制 + 状态机 + 多路选择 |
| 3 | `aes_encipher_block.v` | 设计 | 由 core 实例化为 `enc_block` | 加密数据通路与加密轮 FSM |
| 4 | `aes_decipher_block.v` | 设计 | 由 core 实例化为 `dec_block`，内含 `inv_sbox` | 解密数据通路与解密轮 FSM |
| 5 | `aes_sbox.v` | 设计 | 由 core 实例化为 `sbox_inst`（共享） | 正向 S-box ROM 查表 |
| 6 | `aes_inv_sbox.v` | 设计 | 由 decipher 实例化为 `inv_sbox_inst` | 逆向 S-box ROM 查表 |
| 7 | `aes_key_mem.v` | 设计 | 由 core 实例化为 `keymem` | 密钥扩展 + 轮密钥存储 |
| 8 | `tb_aes.v` | 测试 | 测 `aes` | 顶层 NIST ECB 端到端自检 |
| 9 | `tb_aes_core.v` | 测试 | 测 `aes_core` | 核心组合行为自检 |
| 10 | `tb_aes_encipher_block.v` | 测试 | 测 `aes_encipher_block` | 加密模块逐轮自检 |
| 11 | `tb_aes_decipher_block.v` | 测试 | 测 `aes_decipher_block` | 解密模块逐轮自检 |
| 12 | `tb_aes_key_mem.v` | 测试 | 测 `aes_key_mem` | 密钥扩展自检 |

**层级树（含 testbench 挂载点）**：

```text
tb_aes ──▶ aes
              └─ aes_core ◀── tb_aes_core
                   ├─ aes_encipher_block  ◀── tb_aes_encipher_block
                   │     └─ aes_inv_sbox          （无独立 tb）
                   ├─ aes_decipher_block   ◀── tb_aes_decipher_block
                   ├─ aes_key_mem          ◀── tb_aes_key_mem
                   └─ aes_sbox                     （无独立 tb）
```

**验收标准**：表格 12 行齐全、类型标注正确、层级树能自洽地解释「改了某个设计模块后，应该跑哪些 testbench 做回归」（例如改了 `aes_key_mem.v`，至少要跑 `tb_aes_key_mem.v` 和顶层的 `tb_aes.v`）。

## 6. 本讲小结

- 仓库根目录有 `rtl/`（源码）、`Pre-Synthesis Simulation/`（ModelSim 仿真工程，含与 `rtl/` 相同的 `.v` 副本 + 工程产物）、`Project Pics/`（配图）和 `README.md`。
- **读源码看 `rtl/`，跑仿真用 `Pre-Synthesis Simulation/`**；两个目录里的 12 个 `.v` 文件逐字节相同。
- `rtl/` 下共 **12 个** `.v` 文件：**7 个设计模块**（不以 `tb_` 开头，可综合）+ **5 个测试平台**（以 `tb_` 开头，仅仿真）。
- 设计模块呈层级结构：`aes` → `aes_core` → {`aes_encipher_block`、`aes_decipher_block`（内含 `aes_inv_sbox`）、`aes_key_mem`、共享的 `aes_sbox`}。
- 测试采用分层策略，几乎每个设计模块都有对应 `tb_<module>.v`，唯独两个 S-box 没有独立 testbench。
- 工程约定：文件头注释 + `module` 声明是每个模块的「身份证」，定位职责时先读它们。

## 7. 下一步学习建议

本讲让你在「地图」层面看清了仓库。接下来建议：

- **u1-l3（Verilog 代码风格与寄存器模式）**：进入 `rtl/aes.v`、`rtl/aes_core.v` 内部，学习本工程统一的 `reg/_new/_we` 写使能寄存器模式和异步低有效复位，为读懂后续所有模块打基础。
- **u1-l4（顶层接口与地址映射）**：基于本讲看到的 `aes.v` 端口，深入 `0x00~0x33` 寄存器地址映射与 CTRL/STATUS 的含义。
- **u1-l5（运行仿真与阅读波形）**：动手用 `Pre-Synthesis Simulation/` 工程或 Icarus Verilog 编译运行 `tb_aes.v`，把本讲列出的测试平台真正跑起来。

如果你急于验证本讲清单的准确性，现在就可以挑一个 testbench（例如 `tb_aes.v`）试着编译运行——这正是 u1-l5 的内容。
