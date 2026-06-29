# 分层测试策略

## 1. 本讲目标

本讲是专家篇的第三篇。在 [u3-l2](u3-l2-verification-and-nist-vectors.md) 里，我们已经分析了**顶层** testbench `tb_aes.v` 如何用 NIST 已知应答把整个 AES 核（从总线接口到密文输出）一次性验证完毕。但只靠一个「大而全」的顶层测试是不够的——一旦某个用例失败，你很难定位是密钥扩展错了、加密通路错了、还是控制状态机错了。

本仓库的作者因此为**每一个设计模块都单独配了一个 testbench**，形成一套**自底向上、逐层隔离**的验证体系。读完本讲，你应当能够：

- 说清楚「分层验证」相对于「只跑顶层仿真」的好处（定位快、可独立驱动、可窥探内部）。
- 看懂 `tb_aes_core`、`tb_aes_encipher_block`、`tb_aes_decipher_block`、`tb_aes_key_mem` 四个 testbench 各自验证哪一层、用什么手段隔离上一层。
- 掌握 `dump_dut_state` 这一通用调试手段——它如何用「层次化引用」窥探 DUT 内部不可见于端口的寄存器。

## 2. 前置知识

阅读本讲前，你需要已经掌握以下内容（来自前置讲义）：

- **模块层级**（[u1-l2](u1-l2-directory-structure.md)）：`aes`（顶层总线 wrapper）→ `aes_core`（调度中枢）→ `aes_encipher_block` / `aes_decipher_block`（加/解密车间）+ `aes_key_mem`（轮密钥仓库）+ 被共享的 `aes_sbox`，以及挂在解密模块内部的 `aes_inv_sbox`。
- **init / next 两段式触发**（[u3-l1](u3-l1-end-to-end-encryption-trace.md)）：密钥扩展在 `init` 阶段一次性完成并存入 `key_mem[0..14]`；加/解密在 `next` 阶段进行。
- **顶层 testbench 的骨架**（[u3-l2](u3-l2-verification-and-nist-vectors.md)）：`cycle_ctr` / `error_ctr` / `tc_ctr` 三个计数器、`write_word` / `read_word` 总线任务、`ecb_mode_single_block_test` 模板、`display_test_result` 汇总。
- **Verilog 仿真基础**（[u1-l5](u1-l5-run-simulation-and-waveforms.md)）：`clk_gen` 自驱动时钟、`reset_dut` 复位、`$display` 打印、`initial` 块驱动。

本讲用到但需新引入的术语：

| 术语 | 含义 |
|------|------|
| **DUT** | Device Under Test，被测器件，即 testbench 里 `dut` 这个实例名指向的模块。 |
| **层次化引用 / XMR** | Cross-Module Reference，用 `dut.signal` 或 `dut.subblock.signal` 跨模块层次去读另一个模块内部的信号。这是**仿真特权**，综合工具不认，只能写在 testbench 里。 |
| **桩（stub）** | 为了让被测模块跑起来，由 testbench 额外例化的「配角」模块。例如 `aes_key_mem` 需要一个外部 S-box，testbench 就自己例化一个真实的 `aes_sbox` 当桩。 |
| **预置激励** | 不经过上一级模块，直接把上一级「本该算出来」的数据（如轮密钥）作为常量数组灌进 DUT。 |

## 3. 本讲源码地图

本讲只读四个 testbench，不改动任何设计源码：

| 文件 | 作用 | 验证的层 |
|------|------|----------|
| [rtl/tb_aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v) | 验证 `aes_core`（含 enc/dec/key_mem/sbox 整簇，但**不含**总线 wrapper） | 中层集成 |
| [rtl/tb_aes_encipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v) | 验证 `aes_encipher_block`（轮密钥预置，外挂一个桩 S-box） | 叶子：加密通路 |
| [rtl/tb_aes_decipher_block.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_decipher_block.v) | 验证 `aes_decipher_block`（轮密钥预置，逆 S-box 在 DUT 内部） | 叶子：解密通路 |
| [rtl/tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) | 验证 `aes_key_mem`（外挂一个桩 S-box，逐把检查轮密钥） | 叶子：密钥扩展 |

两个纯查表模块 `aes_sbox` / `aes_inv_sbox` 没有独立 testbench（见 [u1-l2](u1-l2-directory-structure.md)），它们被间接覆盖：正向 S-box 当作桩被三个 testbench 例化，逆向 S-box 由 `tb_aes_decipher_block` 透过 `aes_decipher_block` 间接验证。

## 4. 核心概念与源码讲解

### 4.1 分层测试总览与共享测试骨架

#### 4.1.1 概念说明

「分层测试」的核心思想是：**每一层只测自己负责的逻辑，把上下游用「桩」或「预置激励」替掉**。这样做的三大好处：

1. **定位快**：顶层 `tb_aes` 失败时，你不知道是密钥扩展、加密还是解密错了；但 `tb_aes_key_mem` 失败，你就知道是密钥扩展的锅。
2. **可独立驱动**：每个叶子 testbench 不依赖 `aes_core` 或 `aes.v`，单独编译几个文件就能跑，迭代快。
3. **可窥探内部**：分层 testbench 通常把 `DEBUG` 打开，逐拍 dump DUT 的内部状态，比看顶层波形更容易找到第一处出错的那一拍。

本仓库的测试层次（自底向上）如下：

| 层 | Testbench | DUT 及其直接依赖 | 隔离上一级的方式 |
|----|-----------|------------------|------------------|
| 叶子 | `tb_aes_encipher_block` | `aes_encipher_block` + 桩 `aes_sbox` | **预置** `key_mem[0..14]` 数组，不经 `aes_key_mem` |
| 叶子 | `tb_aes_decipher_block` | `aes_decipher_block`（内含 `aes_inv_sbox`） | **预置** `key_mem[0..14]` 数组 |
| 叶子 | `tb_aes_key_mem` | `aes_key_mem` + 桩 `aes_sbox` | 不做加/解密，只检查每把轮密钥 |
| 中层 | `tb_aes_core` | `aes_core`（含 enc/dec/key_mem/sbox 整簇） | 不含 `aes.v` 总线 wrapper，直接驱动 `init/next/key` 端口 |
| 顶层 | `tb_aes`（[u3-l2](u3-l2-verification-and-nist-vectors.md)） | `aes.v` 全包 | 无 |

注意：四个分层 testbench **都来自 NIST SP 800-38A** 的同一批标准明文/密文/密钥向量，所以它们彼此交叉印证——同一组数据在叶子层、中层、顶层都得到同一结果，就构成了完整的回归网。

#### 4.1.2 核心流程

四个 testbench 共享同一套骨架，流程几乎一致：

```text
声明 cycle_ctr / error_ctr / tc_ctr 三个计数器
  └─ clk_gen：无敏感列表的 always，自驱动产生周期为 2 的方波
  └─ sys_monitor：每个 CLK_PERIOD 自增 cycle_ctr；若 DEBUG=1 则调 dump_dut_state()

initial 主流程：
  init_sim()        → 清计数器、给所有输入定初值
  dump_dut_state()  → 打印复位前状态
  reset_dut()       → 拉低 reset_n 两个周期再释放
  dump_dut_state()  → 打印复位后状态
  反复调用某 test_xxx() 任务 → 每个 task 内部 wait_ready() 后自检、累加 tc_ctr/error_ctr
  display_test_result() → 据 error_ctr 打印通过/失败汇总
  $finish
```

这套骨架与 `tb_aes.v` 几乎逐字相同（见 [u3-l2](u3-l2-verification-and-nist-vectors.md)），区别只在「DUT 是谁」和「用什么激励驱动它」。

#### 4.1.3 源码精读

四个文件里 `init_sim` / `reset_dut` / `display_test_result` 是孪生兄弟。以 `tb_aes_key_mem` 为例：

[rtl/tb_aes_key_mem.v:162-169](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L162-L169) —— `reset_dut` 把 `reset_n` 拉低两个时钟周期再释放，对应 DUT 的异步低有效复位（[u1-l3](u1-l3-verilog-style-and-register-pattern.md) 讲过的复位风格）。

[rtl/tb_aes_key_mem.v:178-191](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L178-L191) —— `init_sim` 把三个计数器清零、所有输入置 0，确保仿真从确定状态起步。

[rtl/tb_aes_key_mem.v:348-360](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L348-L360) —— `display_test_result` 按 `error_ctr` 是否为 0 决定打印 `All NN test cases completed successfully` 还是失败统计。`NN` 直接等于 `tc_ctr`，可从源码数出。

`sys_monitor` 的 DEBUG 开关是控制日志量的关键，以 `tb_aes_encipher_block` 为例：

[rtl/tb_aes_encipher_block.v:109-117](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L109-L117) —— 每个时钟周期自增 `cycle_ctr`，仅当 `DEBUG=1` 才调 `dump_dut_state()`。`tb_aes_core` 把 `DEBUG` 默认设为 0（安静），另外三个默认为 1（逐拍打印），调试时按需切换。

#### 4.1.4 代码实践

**实践目标**：不跑仿真，仅靠源码读出四个 testbench 各自应报告「多少个用例通过」。

**操作步骤**：
1. 打开 [rtl/tb_aes_key_mem.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v) 的 `initial` 块（L367 起），数 `test_key_128(...)` 与 `test_key_256(...)` 的调用次数。
2. 同法数 [rtl/tb_aes_core.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v) 的 `ecb_mode_single_block_test(...)` 调用次数（L369–L421）。
3. 同法数两个叶子 block testbench 的 `test_ecb_enc` / `test_ecb_dec` 调用次数。

**预期结果**（由源码计数得到，运行待本地验证）：

| Testbench | 用例计数 | 预期汇总行 |
|-----------|----------|-----------|
| `tb_aes_key_mem` | 5 个 AES-128 + 4 个 AES-256 = **9** | `*** All 09 test cases completed successfully` |
| `tb_aes_encipher_block` | 4 个 128-enc + 4 个 256-enc = **8** | `*** All 08 test cases completed successfully` |
| `tb_aes_decipher_block` | 4 个 128-dec + 4 个 256-dec = **8** | `*** All 08 test cases completed successfully` |
| `tb_aes_core` | TC 01–08（128）+ TC 10–17（256）= **16** | `*** All 16 test cases completed successfully` |

#### 4.1.5 小练习与答案

**练习 1**：为什么四个 testbench 都要各自重复一份 `clk_gen`，而不是抽到一个公共文件里？
**答案**：每个 testbench 是**独立的顶层模块**（`module tb_xxx;` 没有端口），仿真器一次只编译运行一个顶层；公共时钟若放别的文件无法被「共享实例」。复制一份 `clk_gen` 是 Verilog 单顶层仿真的常规做法，代价仅是几行重复代码。

**练习 2**：`display_test_result` 用 `tc_ctr`（执行数）和 `error_ctr`（失败数）两个计数器，为什么不用一个「成功数」？
**答案**：分离「执行了多少」与「失败了多少」可以在部分失败时同时看到两个数字，便于判断是「全挂」还是「挂了一半」。若只存成功数，失败时你只知道成功数不为满，信息量更少。

---

### 4.2 core 测试：tb_aes_core（中层集成）

#### 4.2.1 概念说明

`tb_aes_core` 验证的是 `aes_core` 这一层——它把加密车间、解密车间、密钥仓库、共享 S-box **整簇**例化进来（因为 `aes_core` 内部就例化了这四个子模块），但**故意不包含 `aes.v` 总线 wrapper**。也就是说，它跳过了「主机写寄存器 → `api` 译码 → 落地 CTRL/CONFIG」这条链（见 [u1-l4](u1-l4-top-interface-and-address-map.md)），直接驱动 `aes_core` 的原生端口 `init / next / key / keylen / encdec / block`。

这样设计的好处：`aes_core` 的算法行为可以脱离总线时序被独立验证；而总线 wrapper 的正确性由顶层 `tb_aes` 单独负责。职责切得很干净。

#### 4.2.2 核心流程

```text
initial aes_core_test:
  装载 NIST AES-128 / AES-256 密钥、4 组明文、对应的 128/256 期望密文
  init_sim → dump → reset_dut → dump
  对每个用例调用 ecb_mode_single_block_test(tc_number, encdec, key, keylen, block, expected):
      1. tb_key=key; tb_keylen=keylen; tb_init=1; 等 2 拍; tb_init=0; wait_ready()   // init 阶段：密钥扩展
      2. dump_keys()                                                                   // 窥探扩展出的 15 把轮密钥
      3. tb_encdec=encdec; tb_block=block; tb_next=1; 等 2 拍; tb_next=0; wait_ready() // next 阶段：加/解密
      4. 若 tb_result == expected → 通过；否则 error_ctr++ 并打印期望/实测
  display_test_result → $finish
```

`tb_aes_core` 用 16 个用例（128 位加解密各 4、256 位加解密各 4）覆盖正反两个方向。

#### 4.2.3 源码精读

[rtl/tb_aes_core.v:53-67](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L53-L67) —— 例化 DUT 为 `aes_core dut(...)`，直接连 `init/next/key/keylen/encdec/block/result/ready` 等原生端口，**没有** `cs/we/address` 这些总线信号——这正是它与顶层 `tb_aes` 的本质区别。

[rtl/tb_aes_core.v:261-308](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L261-L308) —— `ecb_mode_single_block_test` 任务。注意它与顶层 `tb_aes` 里同名任务（[u3-l2](u3-l2-verification-and-nist-vectors.md)）的差异：这里没有 `write_word`/`init_key`，而是直接赋值端口 + `wait_ready()` 轮询 `ready` 握手（比顶层固定延时更精确）。L293-L306 是自检：`tb_result == expected` 则通过，否则 `error_ctr++`。

[rtl/tb_aes_core.v:338-354](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L338-L354) —— NIST 向量定义。`nist_aes128_key` / `nist_aes256_key` 与顶层 `tb_aes` 用的是**同一批**密钥；4 组 `nist_plaintext` 和 8 组期望密文也都与 NIST SP 800-38A 附录 F 的 ECB 例题一致，所以这一层和顶层互相印证。

[rtl/tb_aes_core.v:131-151](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L131-L151) —— `dump_keys` 任务，用层次化引用 `dut.keymem.key_mem[i]`（`i` 从 0 到 14）把 `aes_core` 内部 `keymem` 实例的轮密钥数组逐把打印出来。注意实例名是 `keymem`（见 [rtl/aes_core.v:121](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L121) 里 `aes_key_mem keymem(`），不是 `key_mem`。这是本层独有的「窥探密钥仓库」调试点。

#### 4.2.4 代码实践

**实践目标**：把 `tb_aes_core` 跑起来，确认 16 个用例全过；并用 `dump_keys` 观察一次密钥扩展的中间产物。

**操作步骤**（命令行 iverilog，因为 ModelSim 的 `simulation.mpf` 把路径写死成作者 Windows 绝对路径，换机会失效——见 [u1-l5](u1-l5-run-simulation-and-waveforms.md)）：

```bash
# 进入仓库根目录
iverilog -g2012 -o sim_core.vvp \
  rtl/aes_sbox.v \
  rtl/aes_inv_sbox.v \
  rtl/aes_encipher_block.v \
  rtl/aes_decipher_block.v \
  rtl/aes_key_mem.v \
  rtl/aes_core.v \
  rtl/tb_aes_core.v
vvp sim_core.vvp | tail -n 40
```

> 文件清单说明：`aes_core` 内部例化了 enc/dec/key_mem/sbox 整簇（见 [rtl/aes_core.v:86-138](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L86-L138)），其中 `aes_decipher_block` 又内含 `aes_inv_sbox`，所以必须把这 6 个设计文件连同 testbench 一起编译。

**需要观察的现象**：每个用例后会打印 `Key expansion done` 紧跟 `dump_keys()` 输出的 15 把 `key[00]..key[14]`；末尾出现 `*** All 16 test cases completed successfully`。

**预期结果**：`error_ctr == 0`，汇总行显示 16 个用例通过。实际运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`tb_aes_core` 的 `dump_dut_state` 里（[rtl/tb_aes_core.v:118-121](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L118-L121)）只 dump 了 `dut.enc_block.enc_ctrl_reg` 与 `dut.enc_block.round_ctr_reg`。当你跑**解密**用例时，这会有什么影响？
**答案**：解密用例走的是 `dec_block`，但 dump 里没有 `dec_block.dec_ctrl_reg`，所以在解密失败时这个 dump 帮不上忙——你只能看到 enc_block 的（空闲）状态。要调试解密，应改用 `tb_aes_decipher_block`（它的 `dump_dut_state` 专门 dump `dec_ctrl_reg`，见 4.3 节）。这是分层测试的另一个价值：每层 testbench 的 dump 都为「本层最关心的那个模块」量身定制。

**练习 2**：`ecb_mode_single_block_test` 用 `wait_ready()` 轮询 `ready`，而顶层 `tb_aes` 用固定延时（[u3-l2](u3-l2-verification-and-nist-vectors.md)）。哪种更好？
**答案**：`wait_ready()` 更精确——它跟 DUT 实际完成时刻对齐，DUT 变快变慢都能正确收尾；固定延时则无法区分「DUT 慢」与「DUT 错」（DUT 还没算完就去读，读到的是旧值，被误判为错误）。`tb_aes_core` 这种「靠近算法」的 testbench 用握手更合适。

---

### 4.3 encipher/decipher 测试：叶子级通路隔离

#### 4.3.1 概念说明

`tb_aes_encipher_block` 和 `tb_aes_decipher_block` 是最纯粹的两个**叶子** testbench：它们只验证单个加/解密车间，连 `aes_key_mem` 都不要。

关键隔离手段是**预置轮密钥数组**：作者直接在 testbench 里声明一个 `reg [127:0] key_mem [0:14]`，把 NIST 标准密钥扩展出来的 11 把（AES-128）或 15 把（AES-256）轮密钥**当作已知常量**逐把写进去，再用一句 `assign tb_round_key = key_mem[tb_round]` 按轮号选出来喂给 DUT。于是加/解密模块被测时**完全不依赖**密钥扩展模块的正确性——哪怕 `aes_key_mem` 有 bug，这两个 testbench 照样能独立判定加/解密通路对不对。

两个 testbench 的细微差别在于逆 S-box 的来源：

- **encipher**：DUT 不含 S-box，需要外挂一个真实的 `aes_sbox` 当桩（DUT 通过 `sboxw`/`new_sboxw` 端口与它对话）。
- **decipher**：DUT 内部已例化 `aes_inv_sbox inv_sbox_inst`（见 [rtl/aes_decipher_block.v:205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)），所以 testbench 不需要再挂桩。

#### 4.3.2 核心流程

两个 testbench 的 `initial` 主流程几乎对称：

```text
装载 NIST 4 组明文、对应的 4 组期望密文（128）/ 4 组期望密文（256）
init_sim → dump → reset_dut → dump

# AES-128 阶段
把 key_mem[0..10] 装入 NIST AES-128 的 11 把轮密钥（key_mem[11..14] 填 0 占位）
对 4 组明文各调 test_ecb_enc(AES_128_BIT_KEY, 明文, 期望密文)

# AES-256 阶段
把 key_mem[0..14] 装入 NIST AES-256 的 15 把轮密钥
对 4 组明文各调 test_ecb_enc(AES_256_BIT_KEY, 明文, 期望密文)

display_test_result → $finish
```

`test_ecb_enc` / `test_ecb_dec` 内部只做「设 keylen → 写 block → 拉高一拍 next → wait_ready → 比对 `tb_new_block`」，没有 init 阶段，因为轮密钥已经预置好了。

#### 4.3.3 源码精读

[rtl/tb_aes_encipher_block.v:52](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L52) 与 [rtl/tb_aes_encipher_block.v:58](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L58) —— 声明 `key_mem [0:14]` 数组并用 `assign tb_round_key = key_mem[tb_round]` 按轮号选密钥。这两行就是「绕开 `aes_key_mem`」的全部魔法。

[rtl/tb_aes_encipher_block.v:65-88](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L65-L88) —— 先例化桩 `aes_sbox sbox(...)`，再例化 DUT `aes_encipher_block dut(...)`。注意 DUT 的 `sboxw`/`new_sboxw` 端口连到 testbench 的同名 wire，而这个 wire 同时接桩 sbox——形成「DUT ↔ 桩 sbox」的对话回路，复现了真实核里 enc_block 与共享 sbox_inst 的关系（见 [u2-l1](u2-l1-aes-core-control-fsm.md) 的 `sbox_mux`）。

[rtl/tb_aes_encipher_block.v:337-351](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L337-L351) —— 预置 AES-128 的 11 把 NIST 轮密钥（与 `tb_aes_key_mem` 里 `nist_key128` 扩展出的期望值逐把相同，见 4.4 节）。`key_mem[11..14]` 填 0 占位，因为 AES-128 只用到前 11 把。AES-256 的 15 把在 [rtl/tb_aes_encipher_block.v:360-374](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L360-L374) 装入。

[rtl/tb_aes_encipher_block.v:248-283](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L248-L283) —— `test_ecb_enc` 任务：设 keylen、写 block、`tb_next=1` 维持 2 拍后清零、再等 2 拍、`wait_ready`、比对 `tb_new_block == expected`。解密侧的 `test_ecb_dec` 结构完全相同（[rtl/tb_aes_decipher_block.v:233-268](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_decipher_block.v#L233-L268)），只是输入是密文、期望是明文。

> **读源码时的诚实提醒**：两个 block testbench 里，AES-256 的 4 条期望密文都写成了 `255'h...` 字面量（[rtl/tb_aes_encipher_block.v:320-323](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L320-L323) 与 [rtl/tb_aes_decipher_block.v:305-308](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_decipher_block.v#L305-L308)），但赋值给的是 128 位 `reg`。因为这些常量本身只有 32 个十六进制位（= 128 位），高位被截掉的是 0，结果与 `128'h` 等价、无害。这属于源码里的小笔误，运行行为正确，但读的时候别被 `255` 误导。**待本地验证**。

#### 4.3.4 代码实践

**实践目标**：单独编译运行 `tb_aes_encipher_block`，验证加密车间在「不依赖 `aes_core` 与 `aes_key_mem`」的前提下能独立跑通 8 个 NIST 用例。

**操作步骤**：

```bash
iverilog -g2012 -o sim_enc.vvp \
  rtl/aes_sbox.v \
  rtl/aes_encipher_block.v \
  rtl/tb_aes_encipher_block.v
vvp sim_enc.vvp | tail -n 25
```

> 文件清单只需 3 个：`aes_sbox`（当桩）+ `aes_encipher_block`（DUT）+ testbench。`aes_encipher_block` 不例化任何子模块（见 [u2-l4](u2-l4-encipher-datapath-functions.md)），它通过端口外接 S-box，所以无需 `aes_core`。

**需要观察的现象**：因为 `DEBUG=1`（[rtl/tb_aes_encipher_block.v:17](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L17)），`sys_monitor` 会逐拍 dump 内部状态，你能看到 `enc_ctrl` 在 `IDLE→INIT→SBOX→MAIN` 之间跳转、`round_ctr` 从 0 涨到 10（128）/14（256）、`sword_ctr` 在 0–3 间循环（逐字 SubBytes 的 4 拍，见 [u2-l5](u2-l5-encipher-round-fsm.md)）。末尾出现 `*** All 08 test cases completed successfully`。

**预期结果**：8 个用例全过，`error_ctr == 0`。实际运行结果**待本地验证**。

**对比练习（可选）**：把同一组 AES-128 轮密钥在 `tb_aes_encipher_block` 的 [L337-347](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L337-L347) 与 `tb_aes_key_mem` 的 [L490-501](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L490-L501) 对照，确认两边写的是同一批常量——这就是「叶子层彼此交叉印证」的体现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tb_aes_encipher_block` 要自己例化一个 `aes_sbox`，而 `tb_aes_decipher_block` 不用？
**答案**：`aes_encipher_block` 的设计是「S-box 外接」（通过 `sboxw`/`new_sboxw` 端口与外部对话，见 [u2-l4](u2-l4-encipher-datapath-functions.md)），所以在真实核里它共享 `aes_core` 的那个 `sbox_inst`；脱离了 `aes_core` 测试时，这个外部依赖没人提供，testbench 必须自己挂一个真实的 `aes_sbox` 当桩。而 `aes_decipher_block` 内部已经例化了私有的 `aes_inv_sbox inv_sbox_inst`（[rtl/aes_decipher_block.v:205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205)），自给自足，无需挂桩。

**练习 2**：如果 `aes_key_mem` 里有一个 bug 导致第 5 把轮密钥算错，`tb_aes_encipher_block` 还能通过吗？
**答案**：能。因为 `tb_aes_encipher_block` 的轮密钥是**直接预置的常量**（L337 起），与 `aes_key_mem` 毫无关系。这正是叶子隔离的意义：加/解密通路的正确性与密钥扩展的正确性被解耦验证。要抓密钥扩展的 bug，得靠 `tb_aes_key_mem`（下一节）。

---

### 4.4 key_mem 测试：密钥扩展模块的独立验证

#### 4.4.1 概念说明

`tb_aes_key_mem` 专测 `aes_key_mem`——把主密钥喂进去，触发 `init`，等密钥扩展 FSM 跑完（见 [u2-l3](u2-l3-key-expansion-and-round-key-mem.md) 的 IDLE/INIT/GENERATE/DONE 四状态机），然后**逐把**读回 `key_mem[0..14]` 与期望值比对。

它和加/解密叶子 testbench 一样需要外挂一个桩 `aes_sbox`（因为 `aes_key_mem` 做 SubWord 时也通过 `sboxw`/`new_sboxw` 端口借用外部 S-box）。它的独特之处在于检查方式：不是看「一个 128 位结果对不对」，而是把 11 把（128）或 15 把（256）轮密钥**每一把**都核对一遍——这是定位「密钥扩展在第几轮走偏」的最直接手段。

#### 4.4.2 核心流程

```text
装载多组测试密钥（含 NIST AES-128/256 标准密钥）及其全部期望轮密钥
init_sim → dump → reset_dut → dump

对每组密钥调用 test_key_128(...) 或 test_key_256(...):
    tb_key = key; tb_keylen = ...; tb_init = 1; 等 2 拍; tb_init = 0
    wait_ready()                                    // 等密钥扩展 FSM 跑完
    for key_nr in 0..10 (或 0..14):
        check_key(key_nr, expected[key_nr])         // 设 tb_round=key_nr，读回 tb_round_key 比对

display_test_result → $finish
```

`check_key` 是本 testbench 的核心检查单元：把轮号写到 `tb_round` 端口，下一个周期 `aes_key_mem` 的纯组合读出对应轮密钥（见 [u2-l3](u2-l3-key-expansion-and-round-key-mem.md) 的 `assign round_key = key_mem[round]`），与期望比对。

#### 4.4.3 源码精读

[rtl/tb_aes_key_mem.v:56-73](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L56-L73) —— 例化 DUT `aes_key_mem dut(...)` 与桩 `aes_sbox sbox(...)`。L72 的注释 `// The DUT requirees Sboxes.`（原文拼写）点明了挂桩理由。

[rtl/tb_aes_key_mem.v:219-237](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L219-L237) —— `check_key` 任务：设 `tb_round = key_nr`，等 1 个周期让组合读出稳定，再比对 `tb_round_key == expected`，命中打印 `key 0x0N matched`，否则 `error_ctr++` 并打印期望/实测。

[rtl/tb_aes_key_mem.v:246-284](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L246-L284) —— `test_key_128` 任务：触发 init、`wait_ready`、然后对 `0..10` 共 11 把轮密钥逐个 `check_key`。AES-256 版本 `test_key_256`（[L293-340](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L293-L340)）结构相同，只是检查 15 把、且参数列表更长——作者在注释里自嘲 `Due to array problems, the result check is fairly ugly`（因数组传参不便，只能把 11/15 个期望值逐个列成参数）。

[rtl/tb_aes_key_mem.v:490-507](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L490-L507) —— NIST AES-128 标准密钥 `2b7e1516...4f3c` 及其 11 把期望轮密钥。这 11 把与 `tb_aes_encipher_block` 预置的 [L337-347](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L337-L347) 完全一致——一边是「扩展算法算出来的」，一边是「直接写死的」，两边对上就同时证明了密钥扩展与加/解密通路都正确。NIST AES-256 标准密钥在 [L585-606](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L585-L606)。

#### 4.4.4 代码实践

**实践目标**：单独编译运行 `tb_aes_key_mem`，验证密钥扩展模块脱离 `aes_core` 后能独立正确生成全部轮密钥。

**操作步骤**：

```bash
iverilog -g2012 -o sim_key.vvp \
  rtl/aes_sbox.v \
  rtl/aes_key_mem.v \
  rtl/tb_aes_key_mem.v
vvp sim_key.vvp | grep -E 'matched|Error|All .* test cases'
```

> 文件清单 3 个：`aes_sbox`（桩）+ `aes_key_mem`（DUT）+ testbench。`aes_key_mem` 不例化子模块（见 [u2-l3](u2-l3-key-expansion-and-round-key-mem.md)），S-box 经端口外接。

**需要观察的现象**：`grep` 后能看到形如 `** key 0x00 matched expected round key.` 的大量行（5 组 AES-128 × 11 把 + 4 组 AES-256 × 15 把 = 55 + 60 = **115 次** `check_key` 调用，可从源码数出）。任一不匹配会打印 `** Error: key 0x0N did not match ...`。末尾出现 `*** All 09 test cases completed successfully`（注意这里 `09` 是 `test_key_128/256` 的调用组数，不是 115）。

**预期结果**：`error_ctr == 0`，9 组密钥全部通过。实际运行结果**待本地验证**。

> 语义提示：`tc_ctr`（=9）计的是「测了几组主密钥」，`error_ctr` 累计的是「有几把轮密钥不匹配」。`display_test_result` 只看 `error_ctr` 是否为 0，所以哪怕一把不匹配也会如实报告失败。

#### 4.4.5 小练习与答案

**练习 1**：`check_key` 里为什么要 `#(CLK_PERIOD)` 等一拍才比对（[L222](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L222)）？
**答案**：`tb_round` 是组合输入，`round_key` 是 `aes_key_mem` 里 `assign round_key = key_mem[round]` 的纯组合输出（见 [u2-l3](u2-l3-key-expansion-and-round-key-mem.md)）。改了 `tb_round` 后输出在当拍就应稳定，但等一个 `CLK_PERIOD` 是为了把「新的 `tb_round`」确实推过一个时钟沿、并让 `sys_monitor` 的 dump 反映这一拍，便于观察。本质上是给组合路径留一个确定采样点。

**练习 2**：为什么 `tb_aes_key_mem` 不需要 `tb_next` 信号？
**答案**：`aes_key_mem` 的职责只是「扩展并存储轮密钥」，不做加/解密；它的控制脉冲只有 `init`（触发扩展），没有 `next`。`next` 是 `aes_encipher_block`/`aes_decipher_block` 才有的「开始处理一个数据块」脉冲。所以 `tb_aes_key_mem` 的端口列表里只有 `init`，没有 `next`（见 [L56-70](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L56-L70)）。

---

### 4.5 dump_dut_state 调试手段：层次化引用窥探内部状态

#### 4.5.1 概念说明

`dump_dut_state` 是贯穿四个 testbench 的**通用调试利器**。它的核心是 Verilog 的**层次化引用（Hierarchical Reference / XMR）**：在 testbench 里用 `dut.信号名` 或 `dut.子模块实例名.信号名` 去读取 DUT 内部**不在端口上**的寄存器与线。

这在仿真里是合法且强大的——你能直接看到 FSM 当前状态、计数器值、中间数据通路上的值，而不必把这些信号引到端口（引到端口会污染综合后的接口）。但它是**仿真特权**：综合工具不认层次化引用，所以这种代码只能出现在 testbench，绝不能进设计源码（[u1-l5](u1-l5-run-simulation-and-waveforms.md) 已强调过）。

四个 testbench 的 `dump_dut_state` 各自为「本层最关心的模块」量身定制了窥探内容：

| Testbench | 窥探的重点内部信号 |
|-----------|---------------------|
| `tb_aes_key_mem` | `key_mem_ctrl_reg`（FSM）、`round_key_update`、`round_ctr_reg`、`rcon_reg`、`prev_key0/1_reg/_new/_we`、`round_key_gen` 里的 `w0..w7`/`rconw`/`tw`/`trw`、`key_mem_new/_we` |
| `tb_aes_encipher_block` | `enc_ctrl_reg`（FSM）、`update_type`、`sword_ctr_reg`、`round_ctr_reg`、`round_logic` 里的 `old_block`/`shiftrows_block`/`mixcolumns_block`/`addkey_*_block` |
| `tb_aes_decipher_block` | `dec_ctrl_reg`（FSM）、`update_type`、`sword_ctr_reg`、`round_ctr_reg`、`round_logic` 里的 `old_block`/`inv_shiftrows_block`/`inv_mixcolumns_block`/`addkey_block` |
| `tb_aes_core` | 顶层端口值 + `enc_block.enc_ctrl_reg`/`round_ctr_reg`（见 4.2 的诚实提醒：不 dump 解密侧） |

注意 `dut.round_logic.xxx`、`dut.round_key_gen.xxx` 这种「跨两级层次」的引用——它直接读到了 DUT 内部某个 `always @*` 块里声明的局部线网（如 `shiftrows_block`），让你能看见一轮变换的**中间产物**，定位「到底是 SubBytes、ShiftRows 还是 MixColumns 算错」。

#### 4.5.2 核心流程

`dump_dut_state` 的调用时机有两种：

1. **被动周期性 dump**：`sys_monitor` 在每个 `CLK_PERIOD` 检查 `DEBUG`，为真就调一次（[tb_aes_encipher_block.v:109-117](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L109-L117)）。适合精细跟踪整条 FSM 轨迹，但日志量大。
2. **主动定点 dump**：在 `initial` 主流程的关键节点（复位前、复位后、密钥扩展完成后）显式调用一次。`tb_aes_core` 还提供 `dump_keys` 作为密钥仓库的专项定点 dump。

还有两个开关控制 dump 行为：

- `DEBUG`（默认 0 或 1，因文件而异）：开关周期性 dump。
- `DUMP_WAIT`（默认 0）：开关 `wait_ready` 忙等期间的 dump——当 DUT 卡住不返回 `ready` 时，打开它能逐拍看到 FSM 是否在空转。
- `SHOW_SBOX`（仅 `tb_aes_key_mem`，默认 0）：额外 dump 桩 sbox 的内部 `tmp_new_sbox0..3`，用于调试 SubWord 的 4 路查表。

#### 4.5.3 源码精读

[rtl/tb_aes_key_mem.v:110-154](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L110-L154) —— 最详尽的 `dump_dut_state`。它分三段打印：`Inputs and outputs`（端口级）、`Internal states`（FSM 与计数器与 `prev_key` 寄存器及 `_new/_we`）、`round_key_gen` 内部的字 `w0..w7` 与修正项 `tw`/`trw`/`rconw`。L122-L141 大量使用 `dut.xxx_reg` / `dut.round_key_gen.xxx` 的层次化引用。

[rtl/tb_aes_key_mem.v:144-152](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L144-L152) —— `SHOW_SBOX` 分支，用 `sbox.tmp_new_sbox0` 等**两级**层次引用（testbench 自己例化的 `sbox` 实例 → 其内部线网），窥探 4 路并行查表的中间结果。

[rtl/tb_aes_encipher_block.v:125-159](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_encipher_block.v#L125-L159) —— 加密侧的 `dump_dut_state`。L148-L153 用 `dut.round_logic.old_block`、`dut.round_logic.shiftrows_block`、`dut.round_logic.mixcolumns_block`、`dut.round_logic.addkey_init_block` 等引用，把一轮四个变换的**中间结果**全部暴露——这是定位「哪一步变换算错」的关键。

[rtl/tb_aes_decipher_block.v:112-144](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_decipher_block.v#L112-L144) —— 解密侧的 `dump_dut_state`，结构与加密对称，把 `shiftrows_block` 换成 `inv_shiftrows_block`、`mixcolumns_block` 换成 `inv_mixcolumns_block`。L131 引用 `dut.tmp_sboxw`（解密模块内部给逆 S-box 的输入），对应 [rtl/aes_decipher_block.v:205](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_decipher_block.v#L205) 的 `inv_sbox_inst`。

[rtl/tb_aes_core.v:223-234](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_core.v#L223-L234) —— `wait_ready` 任务，`DUMP_WAIT` 控制忙等期间是否 dump。当 DUT 因 bug 永不置 `ready` 时，打开它能立刻看到 FSM 卡在哪个状态。

#### 4.5.4 代码实践

**实践目标**：体验 `DEBUG` / `SHOW_SBOX` 开关对调试信息量的影响，并用层次化引用定位一次「中间变换」。

**操作步骤**：

1. 先以默认参数跑 `tb_aes_key_mem`（`DEBUG=1, SHOW_SBOX=0`），观察日志中每个周期的 `Internal states` 段。
2. 把 [rtl/tb_aes_key_mem.v:18](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes_key_mem.v#L18) 的 `SHOW_SBOX` 改为 `1`（这是改 testbench 参数，不是改设计源码，符合本讲约束），重跑，观察新增的 `Sbox functionality` 段中 `tmp_new_sbox0..3` 如何随 `sboxw` 变化。
3. 在 `tb_aes_encipher_block` 的日志里找到某一拍，把 `dut.round_logic.shiftrows_block` 与上一拍的 `old_block` 对照，手工验证 ShiftRows 的字节重排（每行循环左移 0/1/2/3，见 [u2-l4](u2-l4-encipher-datapath-functions.md)）。

**需要观察的现象**：`SHOW_SBOX=1` 后能看见 `sboxw` 的 4 个字节经查表变成 `tmp_new_sbox0..3`，再拼成 `new_sboxw`——这正是 [u2-l2](u2-l2-sbox-and-inverse-sbox-rom.md) 讲的「4 路并行查表」的运行时证据。

**预期结果**：能在日志中读到与设计源码声明一致的中间值。具体数值依赖运行，**待本地验证**。

> 约束提醒：本实践只改 testbench 的 `parameter`，不修改任何 `rtl/` 设计源码，符合「不修改源码」的要求。

#### 4.5.5 小练习与答案

**练习 1**：`dut.round_logic.mixcolumns_block` 这种引用为什么不能出现在设计源码 `aes_encipher_block.v` 里？
**答案**：层次化引用是仿真器提供的「从外部看进模块内部」的能力，综合工具不支持。设计源码里出现 `别的实例.信号` 既不可综合也违反了模块封装。它只能用于 testbench 这种「上帝视角」的观察点。

**练习 2**：`DEBUG` 和 `DUMP_WAIT` 都控制 dump，何时该用哪个？
**答案**：`DEBUG` 控制**周期性** dump（每拍都打），适合跟踪正常流程的细节，但日志爆炸。`DUMP_WAIT` 控制 `wait_ready` **忙等期间**的 dump——只在「DUT 似乎卡住、`ready` 迟迟不来」时打开，能立刻看到 FSM 卡在哪个状态、计数器是否在走，是定位「死循环 / 握手失败」的首选开关。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「故障定位」演练：

**场景**：假设你改动了 `aes_key_mem.v` 里的 `rcon_logic`（[u2-l3](u2-l3-key-expansion-and-round-key-mem.md)），担心它影响整个核。

**任务**：

1. **先跑叶子层** `tb_aes_key_mem`。若失败，直接看 `dump_dut_state` 的 `rcon_reg` 与 `round_key_gen.rconw` 随 `round_ctr_reg` 的变化，定位是第几把轮密钥开始错、是 Rcon 算错还是 SubWord 算错。
2. **再跑加密叶子** `tb_aes_encipher_block`。由于它的轮密钥是预置常量、与 `aes_key_mem` 无关，**应当仍然通过**——这证明你的改动只影响密钥扩展，不影响加/解密通路本身。
3. **再跑中层** `tb_aes_core`。由于它包含真实的 `keymem` 实例，此时**应当失败**，且失败现象与第 1 步一致。
4. **最后跑顶层** `tb_aes`（[u3-l2](u3-l2-verification-and-nist-vectors.md)）。同样应当失败。

**交付物**：写一份简表，记录四层 testbench 的通过/失败结果，并据此推断「bug 被隔离在密钥扩展模块」。这正是分层测试的最大价值——**用通过/失败的组合模式把 bug 锁定到具体模块**，而不是在顶层波形里大海捞针。

> 实际运行结果**待本地验证**。本练习重在方法论，不要求真的引入 bug。

## 6. 本讲小结

- 本仓库为**每个设计模块都配了独立 testbench**：叶子层 `tb_aes_encipher_block` / `tb_aes_decipher_block` / `tb_aes_key_mem`，中层 `tb_aes_core`，加上顶层 `tb_aes`，构成自底向上的验证体系。
- 四个分层 testbench **共享同一套骨架**（`cycle_ctr`/`error_ctr`/`tc_ctr`、`clk_gen`、`sys_monitor`、`init_sim`、`reset_dut`、`display_test_result`），并都用 **NIST SP 800-38A** 的同一批标准向量，彼此交叉印证。
- **叶子隔离的两大手段**：① 预置 `key_mem[0..14]` 常量数组（加/解密 block 测试），绕开 `aes_key_mem`；② 由 testbench 自己例化真实的 `aes_sbox` 当**桩**，满足 DUT 的外部 S-box 依赖。
- `tb_aes_core` 是**中层集成**：包含 enc/dec/key_mem/sbox 整簇，但**不含总线 wrapper**，直接驱动 `init/next/key` 端口，用 `wait_ready` 握手而非固定延时。
- **`dump_dut_state`** 用层次化引用（`dut.signal`、`dut.subblock.signal`）窥探 DUT 内部不可见于端口的 FSM 状态、计数器与变换中间产物，是仿真专属的调试特权；`DEBUG` / `DUMP_WAIT` / `SHOW_SBOX` 三个开关控制其信息量。
- 分层测试的终极价值：**用各层通过/失败的组合模式把 bug 锁定到具体模块**，定位效率远高于只跑顶层仿真。

## 7. 下一步学习建议

- 下一讲 [u3-4 面向 ASIC 的设计取舍](u3-4-asic-design-tradeoffs.md) 会从架构角度讨论「为什么正向 S-box 要在 `aes_core` 里共享、而逆 S-box 私挂」「为什么 SubBytes 要拆成 4 拍」——这些取舍的「面积/时序代价」正是本讲分层 testbench 所验证的对象。
- 建议回头结合 [u2-l3](u2-l3-key-expansion-and-round-key-mem.md)～[u2-l7](u2-l7-decipher-round-fsm.md) 的设计源码，对照本讲各 `dump_dut_state` 窥探的信号名，加深「测试看到的内部状态 = 设计源码里那个寄存器」的对应关系。
- 若想动手扩展，可先尝试 [u3-5 二次开发与扩展实践](u3-l5-customization-and-extensions.md) 里「新增一个 NIST 用例」的任务——届时你会同时用到本讲的分层 testbench 做回归验证。
