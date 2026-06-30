# 仿真验证与 NIST 测试向量

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `tb_aes.v` 这个**自检式（self-checking）testbench** 是如何把一次 AES 加/解密拆成一个个可复用 `task` 的。
- 说出 `write_word` / `read_word` / `write_block` / `read_result` 这些总线访问任务与 u1-l4 讲的地址访问主线如何对应。
- 理解 `init_key`、`ecb_mode_single_block_test`、`aes_test` 三层任务的分工与调用关系。
- 看懂 NIST ECB 已知应答（AES-128 与 AES-256）是如何以常量形式写死、再逐块比对的。
- 掌握 `error_ctr` / `tc_ctr` 计数器与 `display_test_results` 的自检报告机制。
- 诚实地认识到：本 testbench 用**固定延时**而非轮询 `STATUS.ready`，并理解这一取舍的利弊。

## 2. 前置知识

本讲是专家篇的验证专题，默认你已经掌握：

- **u1-l4 / u1-l5**：顶层 `aes.v` 的地址映射（`0x00~0x33`）、`CTRL` 的 `init`/`next` 触发位、`STATUS` 的 `ready`/`valid` 状态位，以及 testbench 如何用 `clk_gen` 产生时钟、`reset_dut` 拉复位。
- **u2-l5**：一次 AES-128 加密约需 51 个时钟周期、AES-256 约 71 个周期（`encipher_ctrl` 状态机的 `1 + num_rounds×5` 拍）。
- **u3-l1**：一次加解密必须分 `init`（密钥扩展，写满 `key_mem`）和 `next`（真正加/解密）两次主机触发。

两个本讲会用到的关键概念：

- **自检式 testbench**：测试平台不只产生激励，还**自行判断结果对错**——把期望值写死，与 DUT（Design Under Test，被测设计）实际输出比较，错就累加错误计数器。这样一次仿真结束就能直接给出“通过/失败”，不需要人去看波形逐个核对。
- **NIST 测试向量**：美国 NIST 在 SP 800-38A 等标准里公布的「给定密钥 + 明文 → 标准密文」已知应答。任何 AES 实现，只要喂入同样的输入能得到同样的输出，就被认为算法实现正确。本工程的 16 组用例正是取自这套权威向量。

## 3. 本讲源码地图

本讲只深入一个文件，但会少量引用顶层来印证接口：

| 文件 | 作用 |
|---|---|
| [rtl/tb_aes.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v) | 顶层 AES 核的自检式 testbench，本讲主角。包含总线访问任务、密钥初始化、单块测试用例、NIST 向量集与结果报告。 |
| [rtl/aes.v](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v) | 顶层 wrapper，提供被测的总线接口。本讲只引用它的 `STATUS`/`CTRL` 读回译码，印证 testbench「本可轮询却选了固定延时」。 |

## 4. 核心概念与源码讲解

### 4.1 自检式 testbench 的整体骨架

#### 4.1.1 概念说明

一个完整的 testbench 通常要做三件事：**产生激励 → 驱动 DUT → 检查结果**。本工程的 `tb_aes` 把这三件事组织成一个清晰的流水：

```
main (initial 块)
  ├─ init_sim()         // 给所有激励信号定初值、清零计数器
  ├─ reset_dut()        // 拉低 reset_n 两个周期再释放
  ├─ aes_test()         // 跑全部 16 组 NIST 用例（本讲核心）
  └─ display_test_results()  // 按 error_ctr 打印通过/失败总结
```

其中 `aes_test` 内部又会调用 `ecb_mode_single_block_test`，后者再调用 `init_key` / `write_block` / `read_result`，而它们最终都落到最底层的 `write_word` / `read_word`。这是一个**自顶向下分层、底层复用**的任务调用树：

```
aes_test
  └─ ecb_mode_single_block_test  (× 16)
       ├─ init_key
       │    └─ write_word (× 8 写密钥 + 写 CONFIG + 写 CTRL.init)
       ├─ write_block
       │    └─ write_word (× 4 写明文)
       ├─ write_word (写 CONFIG + 写 CTRL.next)
       ├─ read_result
       │    └─ read_word (× 4 读结果)
       └─ 比对 result_data == expected
```

#### 4.1.2 核心流程

整个仿真的入口是 `initial` 块 `main`，它顺序调用各阶段，最后用 `$finish` 结束仿真：

```verilog
initial
  begin : main
    $display("   -= Testbench for AES started =-");
    init_sim();
    dump_dut_state();
    reset_dut();
    aes_test();
    display_test_results();
    $display("*** AES simulation done. ***");
    $finish;
  end
```

两个全局计数器贯穿始终，是「自检」的灵魂：

- `tc_ctr`：每进入一个测试用例 `+1`，记录**执行了多少组**用例。
- `error_ctr`：每次实际结果 ≠ 期望值时 `+1`，记录**失败了多少组**。

仿真结束时只要 `error_ctr == 0`，就打印 `All NN test cases completed successfully`。

#### 4.1.3 源码精读

计数器声明与 `main` 入口（注意三个 32 位计数器 `cycle_ctr` / `error_ctr` / `tc_ctr`）：

[rtl/tb_aes.v:70-72](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L70-L72) — 三个全局计数器声明。

[rtl/tb_aes.v:488-506](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L488-L506) — `main` 顺序调用各阶段。

结果报告任务 `display_test_results` 按 `error_ctr` 二选一打印，这是「人看一眼就知道全过没」的关键：

[rtl/tb_aes.v:175-187](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L175-L187) — `error_ctr==0` 打印「All NN test cases completed successfully」，否则打印失败数。

> **一个诚实的提醒（贯穿本讲）**：本 testbench **不轮询** `STATUS.ready`。`init_key` 和 `ecb_mode_single_block_test` 在触发 `init`/`next` 后都用固定延时 `#(100 * CLK_PERIOD)` 等待（见 4.3.3、4.4.3）。由于 AES-256 加密实际只约 71 拍，100 拍留有裕量，所以能稳定通过——但这是「拍脑袋给够时间」而非「握手驱动」。硬件侧 `STATUS` 确实暴露了 `ready`/`valid` 位（见下方 aes.v），testbench 本可以轮询它，只是作者选择了更简单的固定延时。4.4 节会给出一个轮询改写示例。

为印证「硬件确实有 ready/valid 可读」，看顶层 `aes.v` 的 `api` 读回译码：

[rtl/aes.v:224-225](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L224-L225) — `ADDR_STATUS` 读回 `{30'h0, valid_reg, ready_reg}`，即 bit0=ready、bit1=valid。

#### 4.1.4 代码实践

**实践目标**：不运行仿真，仅靠读源码数出 `aes_test` 一共会执行多少组用例、`tc_ctr` 最终是多少、若全部通过 `display_test_results` 打印的数字是什么。

**操作步骤**：

1. 打开 [rtl/tb_aes.v 的 aes_test](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L385-L480)。
2. 数其中 `ecb_mode_single_block_test(...)` 的调用次数。
3. 注意 `ecb_mode_single_block_test` 内部第一句就是 `tc_ctr = tc_ctr + 1`（[第 349 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L349)），所以调用次数 = `tc_ctr` 终值。

**预期结果**：共 **16** 次调用（TC 0x01–0x08 的 AES-128 共 8 组、TC 0x10–0x17 的 AES-256 共 8 组），`tc_ctr = 16`，全过时打印 `*** All 16 test cases completed successfully`。这与你之前在 u1-l5 跑仿真看到的输出一致，且**完全可以从源码数出来，不需要真跑**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `display_test_results` 里的 `error_ctr == 0` 改成 `error_ctr = 0`（少打一个 `=`），会发生什么？
**答案**：`error_ctr = 0` 是**赋值语句**而非比较，它会把错误计数器清零并且恒为「真」，于是无论实际有没有失败，永远打印「All 16 test cases completed successfully」，自检报告彻底失效。这正是为什么比较必须用 `==`。

**练习 2**：`tc_ctr` 在哪一行自增？为什么放在 `ecb_mode_single_block_test` 的开头而不是结尾？
**答案**：在 [第 349 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L349) `tc_ctr = tc_ctr + 1`。放在开头是为了「无论本组后续是成功还是比较失败，都已经把它算作一组已执行用例」，这样 `tc_ctr` 表示的是「尝试过的用例数」，与 `error_ctr`（失败数）配合才能算出通过数。

---

### 4.2 总线访问任务 write_word / read_word（及 block / result）

#### 4.2.1 概念说明

u1-l4 讲过，主机访问 AES 核靠的是一组 32 位内存映射总线：`cs`（片选）、`we`（写使能）、`address`、`write_data`、`read_data`。一次写访问的时序是「拉高 `cs`+`we`、给出地址与数据、维持若干拍、再撤销」；读访问类似但 `we=0`，并在当拍采样 `read_data`。

如果每组用例都把这些信号操作原样写一遍，代码会极度冗长且易错。所以 testbench 把「一次写」「一次读」封装成最底层的两个 `task`：`write_word` 与 `read_word`。所有上层任务都建立在它们之上。这是硬件验证里最典型的**底层复用**思想。

#### 4.2.2 核心流程

**写一个 32 位字**（`write_word`）：

```
tb_address  = address      // 给地址
tb_write_data = word       // 给数据
tb_cs = 1; tb_we = 1       // 拉起写选通
#(2 * CLK_PERIOD)          // 维持 2 个时钟周期（一上一下各一拍）
tb_cs = 0; tb_we = 0       // 撤销
```

**读一个 32 位字**（`read_word`）：

```
tb_address = address
tb_cs = 1; tb_we = 0       // 只读，we=0
#(CLK_PERIOD)              // 等一个时钟周期
read_data = tb_read_data   // 采样 DUT 组合输出的 read_data
tb_cs = 0
```

注意读只等 1 个 `CLK_PERIOD`（因为 u1-l4 指出 `read_data` 是**组合读**，当拍给出），而写等 2 个 `CLK_PERIOD`（确保 `posedge clk` 把数据稳定打入 `reg_update` 寄存器）。

在此基础上，`write_block` 把一个 128 位明文按**大端字序**拆成 4 次写（`BLOCK0` 存最高 32 位字 `[127:96]`，`BLOCK3` 存最低 `[31:0]`）；`read_result` 反向把 4 次 `RESULT` 读拼回 128 位。

#### 4.2.3 源码精读

`write_word`：注意 `address` 形参声明为 `[11:0]`（12 位）但实际只用低 8 位，这是作者留的余量；`#(2 * CLK_PERIOD)` 是写维持时间。

[rtl/tb_aes.v:218-235](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L218-L235) — 写一个字：拉 `cs`+`we`、维持 2 拍、撤销。

`read_word`：把 DUT 的 `tb_read_data` 采样进全局变量 `read_data`，供上层使用。

[rtl/tb_aes.v:260-275](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L260-L275) — 读一个字：`we=0`、等 1 拍、采样进 `read_data`。

`write_block`：4 次写，注意大端字序——`ADDR_BLOCK0` 对应 `block[127:96]`。

[rtl/tb_aes.v:243-250](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L243-L250) — 把 128 位明文拆成 4 个 32 位字写入 `BLOCK0..3`。

`read_result`：与 `write_block` 镜像，把 `RESULT0..3` 拼回 `result_data[127:0]`。

[rtl/tb_aes.v:283-294](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L283-L294) — 读 4 个结果字拼成 128 位。

> 字序自洽很重要：`write_block` 用大端（`BLOCK0`=最高字），顶层 `aes.v` 读 `RESULT` 时也用大端（[rtl/aes.v:232-233](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L232-L233) `RESULT0`→`result_reg[127:96]`），`read_result` 又用大端拼回。三处都是大端，所以一条 NIST 向量可以直接写成 128 位十六进制常量，无需手动调换字节。

#### 4.2.4 代码实践

**实践目标**：搞清楚「一次 `write_word` 在波形上长什么样」与「为什么写要 2 拍、读只要 1 拍」。

**操作步骤**：

1. 在 [write_word](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L218-L235) 中确认 `#(2 * CLK_PERIOD)`。
2. 对照 u1-l4 / u3-l1 讲的顶层 `reg_update`：`block_reg` 等寄存器在 `posedge clk` 才更新（[rtl/aes.v:178-179](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L178-L179)）。
3. 思考：若把写维持改成 `#(CLK_PERIOD)`（只 1 拍），数据能否被可靠打入寄存器？

**需要观察的现象 / 预期结果**：写必须跨过至少一个 `posedge clk` 才能被 `reg_update` 采样；`2 * CLK_PERIOD`（一完整周期）保证 `cs/we/address/write_data` 在上升沿稳定有效。读是组合逻辑（`read_data` 当拍出值），所以 `1 * CLK_PERIOD` 足够采样。若把写缩短到 1 拍且相位不对，可能踩不到上升沿导致漏写——**待本地用波形验证**。

#### 4.2.5 小练习与答案

**练习 1**：`write_block` 把 `block[127:96]` 写到 `ADDR_BLOCK0`。如果有人误把 `ADDR_BLOCK0` 写成 `block[31:0]`，AES 最终密文会变成什么样？
**答案**：明文的 4 个 32 位字顺序被颠倒（大端变小端），等价于对「字节倒序的明文」加密，密文会完全不同，所有 16 组用例都会失败、`error_ctr` 暴涨。这正说明大端字序三处必须一致。

**练习 2**：为什么 `read_word` 里把结果存进全局变量 `read_data` 而不做成 `task` 的输出参数？
**答案**：Verilog-2001 的 `task` 可以有 `output` 参数，但作者选择用全局 `reg read_data` / `result_data` 让 `read_result`（连续 4 次读并拼装）写起来更直观——每次 `read_word` 后立刻把 `read_data` 累加到 `result_data` 的对应位段。这是风格选择，不影响功能。

---

### 4.3 init_key 密钥初始化

#### 4.3.1 概念说明

u3-l1 讲过：一次加解密必须**先 init 再 next**。`init` 阶段做密钥扩展——把 256 位（或 128 位）主密钥展开成全部轮密钥写满 `key_mem[0..num_rounds]`。`init_key` 这个任务就是把「喂密钥 + 设密钥长度 + 触发 init」三步打包成一个可复用的初始化入口。

每个用例在加/解密前都要调一次 `init_key`，因为不同用例可能用不同密钥（AES-128 vs AES-256）。

#### 4.3.2 核心流程

`init_key(key, key_length)` 的执行序列：

```
1. write_word(ADDR_KEY0, key[255:224])  // 写 8 个 32 位密钥字（大端）
   ... 直到 ...
   write_word(ADDR_KEY7, key[31:0])     // AES-128 时低 128 位填 0
2. 按 key_length 写 CONFIG：
     AES_256_BIT_KEY(1) -> ADDR_CONFIG = 8'h02  // bit1(keylen)=1
     AES_128_BIT_KEY(0) -> ADDR_CONFIG = 8'h00  // bit1(keylen)=0
   （注意此时 encdec=0，密钥扩展与方向无关）
3. write_word(ADDR_CTRL, 8'h01)         // CTRL.init 置 1，触发密钥扩展
4. #(100 * CLK_PERIOD)                  // 固定等待 100 拍让扩展跑完
```

两个要点：

- **主密钥始终按 256 位传入**。AES-128 时把高 128 位写成真实密钥、低 128 位填 0（见下方 `nist_aes128_key` 常量末尾一堆 0），核内根据 `keylen` 决定只用高 128 位。
- **`CTRL.init` 是单拍脉冲**。u1-l3/u3-l1 讲过：写 `ADDR_CTRL=8'h01` 后，顶层 `api` 块只在那一拍令 `init_new=1`，下一拍 `init_new` 默认回 0，于是 `init_reg` 只高一个周期。密钥扩展子模块靠这一个脉冲自驱完成全部轮密钥生成。

#### 4.3.3 源码精读

`init_key` 完整任务。注意第 320-327 行用 `if (key_length)` 二选一写 CONFIG，第 329 行写 `CTRL.init`，第 331 行固定延时 100 拍：

[rtl/tb_aes.v:303-333](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L303-L333) — 写 8 个密钥字、按密钥长度写 CONFIG、写 CTRL.init 触发扩展、固定等 100 拍。

> 为什么 CONFIG 在 `init_key` 里只设了 `keylen` 没设 `encdec`？因为密钥扩展与加密方向无关（u2-l3）。`encdec` 留到 `ecb_mode_single_block_test` 里在触发 `next` 之前才设置。

CONFIG 位编码对照（来自 [rtl/aes.v:39-41](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L39-L41)）：bit0 = `encdec`（1=加密、0=解密），bit1 = `keylen`（0=128 位、1=256 位）。所以：

| 场景 | CONFIG 值 | 含义 |
|---|---|---|
| `init_key` AES-128 | `8'h00` | keylen=0, encdec=0 |
| `init_key` AES-256 | `8'h02` | keylen=1, encdec=0 |

#### 4.3.4 代码实践

**实践目标**：验证 AES-128 主密钥在 256 位容器里的存放方式。

**操作步骤**：

1. 看 [aes_test 里 nist_aes128_key 的定义](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L405)：`256'h2b7e151628aed2a6abf7158809cf4f3c00000000000000000000000000000000`。
2. 确认前 32 个十六进制位（128 位）`2b7e151628aed2a6abf7158809cf4f3c` 是真实密钥，后 32 位全 0。
3. 追踪 `init_key` 第 311-318 行：它把 `key[255:224]`（即 `2b7e1516`）写到 `KEY0`，把 `key[31:0]`（全 0）写到 `KEY7`。

**预期结果**：对 AES-128，`KEY4..KEY7` 被写成 0，核内 `keylen=0` 时只消费 `KEY0..KEY3`。这是「128/256 统一用 256 位接口」的设计体现。

#### 4.3.5 小练习与答案

**练习 1**：`init_key` 末尾的 `#(100 * CLK_PERIOD)` 是否真的需要 100 拍？AES-256 密钥扩展实际要多少拍？
**答案**：不需要 100 拍那么多。AES-256 的密钥扩展在 `key_mem` 的 GENERATE 状态每拍生成一把轮密钥，需生成 14 把（u2-l3），加上 INIT/DONE 状态开销，总共远小于 100 拍。100 是一个「给够裕量」的保守值——简单但不精确，这也是固定延时风格的代价。

**练习 2**：如果连续两个用例都调 `init_key` 但密钥不同，第二次 init 之前需要复位 DUT 吗？
**答案**：不需要。`init` 会重新覆盖整个 `key_mem[0..num_rounds]`，旧的轮密钥被新密钥扩展结果覆盖。所以可以连续切密钥而不必每次复位——这正是 16 组用例能在一次仿真里连续跑的前提。

---

### 4.4 ecb_mode_single_block_test 单块测试用例

#### 4.4.1 概念说明

`ecb_mode_single_block_test` 是**单块 ECB 用例的模板**：给它「用例号、加/解密方向、密钥、密钥长度、输入块、期望输出块」六个参数，它就自动走完「初始化密钥 → 写入块 → 触发 next → 读结果 → 比对」全流程，并自检对错。`aes_test` 只要用不同参数反复调用它 16 次，就完成了全部 NIST 验证。

这是**「参数化模板 + 数据驱动」**的验证思想：流程代码只写一遍，测试数据（密钥、明文、期望密文）作为参数注入。

#### 4.4.2 核心流程

`ecb_mode_single_block_test` 的内部序列：

```
tc_ctr ++                                       // 计一组用例
init_key(key, key_length)                       // 4.3 的密钥初始化
write_block(block)                              // 写明文（或解密时写密文）
dump_dut_state()                                // 可选调试转储

write_word(ADDR_CONFIG, (key_length << 1) + encdec)  // 设置完整 CONFIG（含 encdec）
write_word(ADDR_CTRL, 8'h02)                    // CTRL.next 置 1，触发加/解密
#(100 * CLK_PERIOD)                             // 固定等待 100 拍

read_result()                                   // 读回 128 位结果
if (result_data == expected)  打印成功
else                          打印失败 + error_ctr ++
```

注意一个**微妙的两段式 CONFIG**：

- `init_key` 内部先写了一次 CONFIG（只设 `keylen`，`encdec=0`）。
- 这里在触发 `next` **之前**又写了一次 CONFIG：`(key_length << 1) + encdec`，这次把 `encdec` 也设上。

所以同一个用例里 CONFIG 被写了两遍——第一遍为了让密钥扩展知道密钥长度，第二遍为了让加/解密通路知道方向。两次的 `keylen` 一致，只是第二次补上了 `encdec`。

#### 4.4.3 源码精读

`ecb_mode_single_block_test` 完整任务。注意第 355 行 CONFIG 计算 `(8'h00 + (key_length << 1) + encdec)`，第 356 行 `CTRL=8'h02` 触发 next，第 358 行固定延时，第 362 行比对：

[rtl/tb_aes.v:341-377](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L341-L377) — 单块测试模板：init → 写块 → 设 CONFIG + 触发 next → 等 100 拍 → 读结果 → 比对自检。

CONFIG 计算式的真值表（`key_length` 在 bit1、`encdec` 在 bit0）：

| 方向 encdec | 密钥长度 key_length | CONFIG 值 | 二进制 |
|---|---|---|---|
| 1 (加密) | 0 (AES-128) | `8'h01` | `...01` |
| 1 (加密) | 1 (AES-256) | `8'h03` | `...11` |
| 0 (解密) | 0 (AES-128) | `8'h00` | `...00` |
| 0 (解密) | 1 (AES-256) | `8'h02` | `...10` |

> **轮询改写示例（示例代码，非项目原代码）**：若要把第 358 行的固定延时改成握手驱动，可改成轮询 `STATUS.ready`。原理是 DUT 在 next 完成后会拉高 `ready`（u2-l1）。下面是改写思路（**仅作示意，请勿写入源码**）：
>
> ```verilog
> // 示例代码：用轮询 STATUS.ready 取代 #(100*CLK_PERIOD)
> read_word(ADDR_STATUS);          // 读 STATUS，bit0=ready
> while (!read_data[STATUS_READY_BIT]) begin  // 直到 ready 为 1
>   #(CLK_PERIOD);
>   read_word(ADDR_STATUS);
> end
> ```
>
> 这样无论 DUT 跑 51 拍还是 71 拍都能自适应，鲁棒性更好；但代码更长、且依赖 `ready` 实现正确。作者用固定延时是以「冗余等待」换「代码简洁」。理解这个取舍比记住具体写法更重要。

#### 4.4.4 代码实践

**实践目标**：亲手把一组用例的六个参数填进 `ecb_mode_single_block_test`，理解解密用例的「输入/期望」与加密用例正好相反。

**操作步骤**：

1. 看 [aes_test 第 426-427 行的 AES-128 加密 TC 0x01](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L426-L427)：输入是 `nist_plaintext0`，期望是 `nist_ecb_128_enc_expected0`。
2. 再看 [第 439-440 行的 AES-128 解密 TC 0x05](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L439-L440)：输入是 `nist_ecb_128_enc_expected0`（加密的输出），期望是 `nist_plaintext0`（加密的输入）。

**需要观察的现象 / 预期结果**：解密用例把「加密的密文」当输入，「加密的明文」当期望——即加密 TC 0x01 与解密 TC 0x05 互为逆运算、参数互换。如果硬件加解密都正确，`nist_plaintext0 → enc → expected0 → dec → nist_plaintext0` 形成闭环。这种「加密产出的密文直接喂给解密当输入」的写法，省去了为解密单独准备测试向量。

#### 4.4.5 小练习与答案

**练习 1**：第 358 行 `#(100 * CLK_PERIOD)` 之后直接 `read_result()`。如果某次 DUT 因 bug 实际跑了 120 拍才完成，会发生什么？
**答案**：100 拍时 DUT 尚未完成，`result_reg` 还是中间值（u3-l1 讲过 result 每拍镜像车间工作寄存器），`read_result` 读到的是「半成品」，比对必然失败，`error_ctr` 加 1。这正是固定延时的风险——它无法发现「DUT 慢了」与「DUT 算错了」的区别，只会一律判失败。

**练习 2**：`encdec` 参数取 `AES_ENCIPHER(1'b1)` / `AES_DECIPHER(1'b0)`（[第 63-64 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L63-L64)）。为什么解密是 0 而加密是 1？
**答案**：这只是作者对 CONFIG bit0（`encdec`）的语义约定（见 [aes.v:40](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L40)）：`encdec=1` 表示加密（encipher）、`encdec=0` 表示解密（decipher）。把 `AES_DECIPHER` 命名成 `1'b0`、`AES_ENCIPHER` 命名成 `1'b1` 是为了让调用处写 `AES_ENCIPHER`/`AES_DECIPHER` 而不是裸 `0`/`1`，提高可读性。

---

### 4.5 aes_test NIST 向量集（重点：AES-256 的 4 组）

#### 4.5.1 概念说明

`aes_test` 是把所有测试数据集中声明、再批量调用模板的**数据层**。它声明了两把密钥（AES-128 / AES-256）、4 个明文、4 个 AES-128 期望密文、4 个 AES-256 期望密文，然后用这些常量调用 `ecb_mode_single_block_test` 共 16 次。这些数值取自 **NIST SP 800-38A** 的 ECB 模式示例向量（附录 F.1.1/F.1.2 给 AES-128、F.1.5/F.1.6 给 AES-256），是国际通用的 AES 正确性基准。

本节聚焦**代码实践任务要求的 AES-256 ECB 4 组明文与期望密文**。

#### 4.5.2 核心流程

NIST 用的 4 个明文块对所有密钥长度通用（即 AES-128 和 AES-256 用同一批明文）：

| 编号 | 明文常量 | 值 |
|---|---|---|
| 0 | `nist_plaintext0` | `6bc1bee22e409f96e93d7e117393172a` |
| 1 | `nist_plaintext1` | `ae2d8a571e03ac9c9eb76fac45af8e51` |
| 2 | `nist_plaintext2` | `30c81c46a35ce411e5fbc1191a0a52ef` |
| 3 | `nist_plaintext3` | `f69f2445df4f9b17ad2b417be66c3710` |

AES-256 主密钥：

\[ \text{nist\_aes256\_key} = \texttt{603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4} \]

用这把密钥 ECB 加密上述 4 个明文，NIST 给出的标准密文（即本工程的期望值）：

| 编号 | 期望密文常量 | 值 |
|---|---|---|
| 0 | `nist_ecb_256_enc_expected0` | `f3eed1bdb5d2a03c064b5a7e3db181f8` |
| 1 | `nist_ecb_256_enc_expected1` | `591ccb10d410ed26dc5ba74a31362870` |
| 2 | `nist_ecb_256_enc_expected2` | `b6ed21b99ca6f4f9f153e7b1beafed1d` |
| 3 | `nist_ecb_256_enc_expected3` | `23304b7a39f9f3ff067d8d8f9e24ecc7` |

> 在 ECB 模式下，加密可看作一个确定的纯函数 \( C = E_K(P) \)，每个 128 位明文块 \(P\) 独立映射到密文块 \(C\)，块间互不影响。所以「明文 i → 期望密文 i」是一一对应，没有链接、没有初始向量。

#### 4.5.3 源码精读

数据声明：密钥与明文（明文为 AES-128/256 共用）。

[rtl/tb_aes.v:405-411](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L405-L411) — 声明 AES-128/256 主密钥与 4 个 NIST 明文。

AES-256 的 4 组期望密文声明：

[rtl/tb_aes.v:418-421](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L418-L421) — AES-256 ECB 加密的 4 组标准期望密文。

**AES-256 加密的 4 组调用**（即代码实践任务要找的 4 组）。每一组把一个明文和对应的期望密文喂给 `ecb_mode_single_block_test`：

[rtl/tb_aes.v:455-465](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L455-L465) — TC 0x10–0x13：AES-256 加密的 4 组用例。

每组调用的对应关系（参数顺序：`tc_number, encdec, key, key_length, block, expected`）：

| 用例 TC | encdec | key | key_length | block（输入明文） | expected（期望密文） |
|---|---|---|---|---|---|
| `8'h10` | `AES_ENCIPHER` | `nist_aes256_key` | `AES_256_BIT_KEY` | `nist_plaintext0` | `nist_ecb_256_enc_expected0` |
| `8'h11` | `AES_ENCIPHER` | `nist_aes256_key` | `AES_256_BIT_KEY` | `nist_plaintext1` | `nist_ecb_256_enc_expected1` |
| `8'h12` | `AES_ENCIPHER` | `nist_aes256_key` | `AES_256_BIT_KEY` | `nist_plaintext2` | `nist_ecb_256_enc_expected2` |
| `8'h13` | `AES_ENCIPHER` | `nist_aes256_key` | `AES_256_BIT_KEY` | `nist_plaintext3` | `nist_ecb_256_enc_expected3` |

紧接着还有 4 组 AES-256 **解密**（TC 0x14–0x17），把上面 4 组的「输入/期望」对调——密文当输入、明文当期望：

[rtl/tb_aes.v:468-478](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L468-L478) — TC 0x14–0x17：AES-256 解密的 4 组用例，参数与加密组互逆。

#### 4.5.4 代码实践（本讲指定任务）

**实践目标**：在 `aes_test` 中找到 AES-256 ECB 的 4 组明文与期望密文，说明每组如何调用 `ecb_mode_single_block_test` 完成比对。

**操作步骤**：

1. **定位明文**：在 [第 408-411 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L408-L411) 找到 4 个明文常量 `nist_plaintext0..3`，记下它们的值（见 4.5.2 表）。
2. **定位期望密文**：在 [第 418-421 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L418-L421) 找到 AES-256 的 4 个期望密文 `nist_ecb_256_enc_expected0..3`。
3. **定位调用**：在 [第 455-465 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L455-L465) 找到 TC 0x10–0x13 这 4 次加密调用。
4. **逐组说明比对**：以 TC 0x10 为例——
   - 调用：`ecb_mode_single_block_test(8'h10, AES_ENCIPHER, nist_aes256_key, AES_256_BIT_KEY, nist_plaintext0, nist_ecb_256_enc_expected0)`。
   - `init_key` 用 256 位主密钥做密钥扩展（`keylen=1`）。
   - `write_block(nist_plaintext0)` 把 `6bc1bee2...172a` 写入 BLOCK。
   - 触发 `next` 加密，等 100 拍后 `read_result` 读回实际密文。
   - 在 [第 362 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L362) 比对 `result_data == nist_ecb_256_enc_expected0`（即 `f3eed1bd...81f8`）。
   - 相等则打印 `TC 10 successful`，否则 `error_ctr++`。
   - TC 0x11/0x12/0x13 同理，只是把下标换成 1/2/3，明文与期望密文一一对应。

**预期结果**：若硬件 AES-256 加密实现正确，4 组实际密文分别等于 4 组期望密文，`error_ctr` 不增，仿真结束打印 `All 16 test cases completed successfully`。这 4 组期望值与 NIST SP 800-38A 附录 F.1.6 的官方向量完全一致，因此一旦通过即证明 AES-256 加密数据通路正确。

#### 4.5.5 小练习与答案

**练习 1**：AES-256 的 TC 0x10 加密与 TC 0x14 解密有什么关系？它们的参数如何互逆？
**答案**：互为逆运算。TC 0x10 是 `encdec=ENCIPHER`，输入 `nist_plaintext0`、期望 `nist_ecb_256_enc_expected0`；TC 0x14（[第 468-469 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L468-L469)）是 `encdec=DECIPHER`，输入 `nist_ecb_256_enc_expected0`、期望 `nist_plaintext0`。即把前者的输出当作后者的输入、前者的输入当作后者的期望，形成 \( P \xrightarrow{E_K} C \xrightarrow{D_K} P \) 的闭环。

**练习 2**：如果有人误把 `nist_ecb_256_enc_expected0` 的值抄错一个十六进制位（比如 `f3eed1bd` 抄成 `f3eed1be`），仿真会怎样？
**答案**：TC 0x10 的实际密文（正确值 `f3eed1bd...`）将与错误期望 `f3eed1be...` 不等，[第 362 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L362) 比对失败，打印 `ERROR: TC 10 NOT successful` 并显示 Expected/Got 两行，`error_ctr` 变 1，最终报告变成 `16 tests completed - 01 test cases did not complete successfully`。同时由于 TC 0x14 用同一个 `expected0` 当输入，解密组的输入也会受影响（取决于抄错的是 expected 常量还是别的）——这说明测试向量的正确性本身就是验证的前提。

**练习 3**：为什么 4 个明文 `nist_plaintext0..3` 在 AES-128 和 AES-256 之间可以共用？
**答案**：因为 ECB 单块加密的输入只是一个 128 位明文块，与密钥长度无关；密钥长度只影响密钥扩展的轮数和轮密钥内容，从而影响密文，但不改变明文块本身的格式。所以 NIST 用同一批明文、配不同长度密钥，得到两套不同的期望密文。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「**端到端单用例追踪**」。

**任务**：选定 AES-256 加密用例 **TC 0x10**，对照源码画出它从 `main` 到比对完成的**完整调用与寄存器访问序列表**，并回答三个问题。

**操作步骤**：

1. 从 [main](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L488-L506) 出发，列出 `init_sim → reset_dut → aes_test → display_test_results` 的顺序。
2. 进入 `aes_test` 的 TC 0x10 调用（[第 455-456 行](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/tb_aes.v#L455-L456)），展开 `ecb_mode_single_block_test` 内部。
3. 用一张多列表格列出 TC 0x10 执行过程中，主机依次访问的每一个地址及其操作：

| 步骤 | 来自哪个 task | 地址 | 操作 | 数据/含义 |
|---|---|---|---|---|
| 1 | init_key | KEY0..KEY7 | 写 ×8 | 256 位主密钥（大端） |
| 2 | init_key | CONFIG | 写 | `0x02`（keylen=1） |
| 3 | init_key | CTRL | 写 | `0x01`（触发 init） |
| 4 | init_key | — | 等待 | 100 拍密钥扩展 |
| 5 | write_block | BLOCK0..3 | 写 ×4 | 明文 `6bc1bee2...172a` |
| 6 | ecb_... | CONFIG | 写 | `0x03`（keylen=1, encdec=1） |
| 7 | ecb_... | CTRL | 写 | `0x02`（触发 next 加密） |
| 8 | ecb_... | — | 等待 | 100 拍加密 |
| 9 | read_result | RESULT0..3 | 读 ×4 | 拼成 128 位实际密文 |
| 10 | ecb_... | — | 比对 | 实际 vs `f3eed1bd...81f8` |

4. **回答三个问题**：
   - 步骤 2 和步骤 6 都写了 CONFIG，为什么写两次？值为何不同？（答：第一次只设 keylen 给密钥扩展用，第二次补上 encdec 给加解密通路用。）
   - 步骤 4 和步骤 8 的 100 拍，分别对应硬件哪个子模块在工作？（答：步骤 4 是 `aes_key_mem` 的密钥扩展 FSM，步骤 8 是 `aes_encipher_block` 的 `encipher_ctrl`。）
   - 如果步骤 10 比对成功，`tc_ctr` 和 `error_ctr` 各是多少？（答：`tc_ctr` 在步骤进入时已 `+1`，`error_ctr` 不变。）

**预期结果**：你能用这一张表讲清楚「一组 NIST 用例从头到尾触发了哪些总线访问、等待了哪些硬件阶段、最后如何自检」。这就把 4.1（骨架）、4.2（总线任务）、4.3（init_key）、4.4（模板）、4.5（向量）五个模块贯通成一个完整的验证故事，也为下一讲 u3-l3（分层测试）做好了铺垫。

## 6. 本讲小结

- `tb_aes` 是**自检式 testbench**：用 `tc_ctr` 数执行用例数、`error_ctr` 数失败数，`display_test_results` 据此打印 `All 16 test cases completed successfully` 或失败报告，仿真结束即可定论。
- 任务调用是**自顶向下分层复用**：`aes_test → ecb_mode_single_block_test → init_key/write_block/read_result → write_word/read_word`，底层两个总线任务是所有访问的基石。
- `write_word` 写维持 2 拍（确保打入 `posedge clk` 寄存器），`read_word` 读等 1 拍（组合读当拍出值）；`write_block`/`read_result` 按大端字序拆装 128 位，与顶层 `aes.v` 三处大端自洽。
- `init_key` 把「写 8 字密钥 + 设 keylen + 触发 init + 等 100 拍」打包；`CTRL.init` 是单拍脉冲；主密钥统一用 256 位容器（AES-128 低 128 位填 0）。
- `ecb_mode_single_block_test` 是参数化单块模板：`init → 写块 → 设完整 CONFIG(含 encdec) → 触发 next → 等 100 拍 → 读结果 → 比对自检`；解密用例把加密用例的输入/期望对调，形成加解密闭环。
- `aes_test` 用 NIST SP 800-38A 官方向量驱动 16 组用例：AES-128/256 各 4 组加密 + 4 组解密。**AES-256 加密的 4 组**（TC 0x10–0x13）依次把 `nist_plaintext0..3` 映射到 `nist_ecb_256_enc_expected0..3`。
- **一个必须记住的诚实结论**：本 testbench 用**固定延时 `#(100*CLK_PERIOD)`** 而非轮询 `STATUS.ready`，简洁但不精确、且无法区分「DUT 慢」与「DUT 错」；硬件侧 `STATUS` 确实暴露了 ready/valid，可改写为轮询握手以提升鲁棒性。

## 7. 下一步学习建议

- **u3-l3（分层测试策略）**：本讲只看了顶层 `tb_aes`。下一步去看 `tb_aes_core` / `tb_aes_encipher_block` / `tb_aes_decipher_block` / `tb_aes_key_mem` 这几个**子模块独立 testbench**，理解工程如何「自底向上、逐层隔离」验证每个子模块，以及 `dump_dut_state` 式的层次化调试手段。
- **动手扩展（承接 u3-l5）**：试着在 `aes_test` 里新增一组用例（例如换一把自己的密钥、用 Python/openssl 算出期望密文后填入），或把 4.4.3 的「轮询 `STATUS.ready`」改写思路真的实现一遍，体会固定延时 vs 握手驱动的差异。
- **横向阅读**：把本讲的 NIST 向量与 [NIST SP 800-38A](https://csrc.nist.gov/publications/detail/sp/800-38a/final) 附录 F 的官方表对照，确认 `nist_ecb_256_enc_expected0..3` 逐字节一致——这是「以国际标准校验自己实现」的标准做法。
