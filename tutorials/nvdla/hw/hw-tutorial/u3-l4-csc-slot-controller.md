# CSC：时隙/条带控制器与权重分发

## 1. 本讲目标

本讲聚焦 NVDLA 卷积主流水线的第三级——**CSC（Convolution Slot/Strip Controller，卷积时隙/条带控制器）**。学完后你应该能够：

- 说清 CSC 在 `CDMA→CBUF→CSC→CMAC→CACC` 五级流水中的位置与职责：从 CBUF 读出特征图（dat）与权重（wt），按 MAC 阵列节拍分发给 CMAC。
- 理解 CSC 内部 `regfile + sg + wl + dl + slcg` 的分工：谁管配置、谁是「指挥」、谁搬数据、谁搬权重。
- 掌握 SG（Sequence Generator）如何用「slot 描述符」驱动 WL 与 DL，以及它如何用 CACC 回送的 credit 做反压。
- 看懂一个 slot 内数据包 `sg2dl_pd` 与权重包 `sg2wl_pd` 的字段组装，并指出数据/权重分别送往 `cmac_a` 还是 `cmac_b`。
- 认识 Winograd 变换路径在 CSC 中的实现位置与时钟门控方式。

## 2. 前置知识

阅读本讲前，请先建立以下直觉（已在 u3-l1/u3-l2/u3-l3 讲义中讲过）：

- **卷积主流水线五级**：CDMA 取数 → CBUF 缓冲 → CSC 分发 → CMAC 乘加 → CACC 累加。CSC 夹在片上 SRAM（CBUF）与乘加阵列（CMAC）之间，是「喂」MAC 阵列的那一级。
- **C 维与 K 维**：卷积可看作对输入通道（C 维，reduction/累加维）做加权求和、产生输出通道（K 维）。CMAC 阵列规模 `MAC_ATOMIC_C_SIZE_64 × MAC_ATOMIC_K_SIZE_32`（见 [nv_full.spec:16-17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16-L17)），共 2048 个 INT8 MAC，分 `cmac_a`/`cmac_b` 两半，每半 1024 MAC。
- **CBUF 的 bank 布局**（u3-l3）：bank 0 数据专用、bank 15 权重及权重掩码位图（wmb）专用、bank 1~14 数据与权重共享；CSC 通过三个读口访问 CBUF：`sc2buf_dat_rd`（数据，12 位地址）、`sc2buf_wt_rd`（权重，12 位地址）、`sc2buf_wmb_rd`（权重掩码位图，8 位地址），均 1024 位宽。
- **wmb（weight mask bitmap）**：压缩权重配套的位图，标记哪些权重位置有效（非零），由 CDMA 的 WT 通路一并搬入 CBUF bank 15。
- **影偶（shadow）配置**（u2-l3）：引擎用 `dual_reg` 的 d0/d1 两组寄存器轮换，CPU 写 producer 组、引擎运行时读 consumer 组，实现无停顿更新配置。
- **反压与 credit**：下游累加器 CACC 通过 `accu2sc_credit` 向 CSC 回送信用，CSC 据此决定是否继续生成数据节拍，避免淹没累加器。

> 术语提示：本讲频繁出现 **slot（时隙）** 与 **stripe（条带）**。slot 是 CSC 一次分发动作的最小单位（一拍数据 + 对应权重的描述）；stripe 是沿输出宽度方向切出的一段，多个 stripe 拼成一个通道的输出。CSC 名字里的 "Slot/Strip" 正源于此。

## 3. 本讲源码地图

CSC 的全部源码集中在 `vmod/nvdla/csc/` 目录。本讲涉及的关键文件如下：

| 文件 | 作用 | 规模 |
|------|------|------|
| [NV_NVDLA_csc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v) | CSC 顶层，例化 regfile/sg/wl/dl/slcg，串接对外端口 | 约 2060 行（多为端口） |
| [NV_NVDLA_CSC_regfile.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_regfile.v) | 寄存器文件 + 影偶切换大脑（手写） | 1140 行 |
| [NV_NVDLA_CSC_sg.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v) | 序列发生器，生成 slot 描述符 | 9444 行 |
| [NV_NVDLA_CSC_wl.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_wl.v) | 权重加载器，读 wt/wmb 喂 CMAC | 13775 行 |
| [NV_NVDLA_CSC_dl.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v) | 数据加载器，读 dat 喂 CMAC，含 Winograd | 23405 行 |
| NV_NVDLA_CSC_single_reg.v / _dual_reg.v | 自动生成的即时/影偶寄存器组 | — |
| NV_NVDLA_CSC_slcg.v | 二级时钟门控单元 | — |
| NV_NVDLA_CSC_WL_dec.v / _pra_cell.v / SG_*_fifo.v | wl 的权重解码、dl 的 Winograd 预加单元、sg 的描述符 FIFO | — |

> 说明：`sg`/`wl`/`dl` 三个文件体量巨大且由工具展开生成，阅读时不要逐行读，应先抓「模块端口 + 子模块实例 + 关键状态机/数据包」三条主线。本讲的源码精读即按此策略展开。

## 4. 核心概念与源码讲解

### 4.1 CSC 顶层：节拍分发的总指挥

#### 4.1.1 概念说明

CSC 要解决的核心问题是：**如何把 CBUF 里「成块」存放的特征图与权重，重新组织成 CMAC 阵列「每拍所需」的数据/权重对，并保证两者在时间上严格对齐。**

这里有一个关键的不匹配：

- CBUF 的存储是按卷积窗口预取的 2D 数据块（CDMA 负责搬入），地址空间「扁平」。
- CMAC 阵列每拍需要的，是按 MAC 阵列的 C 维（输入通道原子）× K 维（输出通道原子）展开的一组标量，且要配合卷积窗口在输入图上的滑动、padding、stride、dilation 重新排列。

CSC 就是这个「重排 + 节拍对齐」的引擎。它内部用一个「指挥」(SG) 生成时序描述符，再让两个「搬运工」(DL 搬数据、WL 搬权重) 按描述符去 CBUF 取数并送给 CMAC。

#### 4.1.2 核心流程

CSC 顶层的组成与数据流可概括为下图（文字版）：

```
        CSB 配置总线
            │
      ┌─────▼─────┐
      │  regfile   │  影偶配置 → reg2dp_* （conv_mode, 精度, 尺寸, bank, pad …）
      └─────┬──────┘
            │ reg2dp_*
      ┌─────▼─────┐    sg2dl_pd[30:0]   ┌─────────┐
      │     sg     │──────────────────▶│   dl    │── sc2buf_dat_rd ──▶ CBUF(dat)
      │ (序列发生器)│    sg2wl_pd[17:0]   │(数据加载)│◀─ sc2buf_dat_rd_data(1024b)
      │            │──────────────────┐ └────┬────┘
      └─────┬──────┘                  │      │ sc2mac_dat_a/b (128×8b, 广播)
            │ sc_state                 │      ▼
            │                          │ ┌─────────┐
            │                          └▶│   wl    │── sc2buf_wt_rd / wmb_rd ──▶ CBUF(wt/wmb)
            │                            │(权重加载)│◀─ sc2buf_wt/wmb_rd_data(1024b)
            │                            └────┬────┘
            │                                 │ sc2mac_wt_a/b (128×8b, 按 sel 分流)
            ▼                                 ▼
   accu2sc_credit (反压) ◀──────── CMAC（cmac_a / cmac_b）
```

要点：

1. **regfile** 接收 CSB 配置，做影偶切换，输出 `reg2dp_*` 配置总线给 sg/wl/dl。
2. **sg** 是节拍源：它根据配置（atomics、batches、尺寸、stride…）与反压（`accu2sc_credit`、`cdma2sc_*_pending`）生成一串 **slot 描述符**，分别经 `sg2dl_pd`/`sg2wl_pd` 送往 dl 与 wl。
3. **dl** 消费 `sg2dl_pd`，从 CBUF 读 dat，做 padding/重排（含 Winograd 变换），把 128 字节特征数据**广播**给 `cmac_a` 与 `cmac_b`。
4. **wl** 消费 `sg2wl_pd`，从 CBUF 读 wt 与 wmb，做权重解码/掩码，把 128 字节权重**按 sel 分流**给 `cmac_a` 与 `cmac_b`。
5. **slcg**：四个二级时钟门控单元，分别给 sg/wl/dl 以及 Winograd 子路径供门控时钟，空闲时关钟省电。

#### 4.1.3 源码精读

CSC 顶层模块声明与端口极多（主要是 128 路 `sc2mac_dat_a/b_data*` 与 `sc2mac_wt_a/b_data*`），但模块体只例化了 5 类子模块。其骨架见：

模块声明与三类 CBUF 读口（数据 12 位地址、wmb 8 位地址、权重 12 位地址，均 1024 位数据）：

[NV_NVDLA_csc.v:615-631](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L615-L631) — CSC 对 CBUF 的三个读口：`sc2buf_dat_rd`、`sc2buf_wmb_rd`、`sc2buf_wt_rd`，地址宽度分别 12/8/12 位，数据均 1024 位，与 u3-l3 讲的 CBUF bank 布局对应（wmb 只在 bank 15，故地址位宽更窄）。

四个子模块例化点：

[NV_NVDLA_csc.v:1232-1280](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L1232-L1280) — 例化 `NV_NVDLA_CSC_regfile u_regfile`，把 CSB 请求接入、把 `reg2dp_*` 配置扇出。

[NV_NVDLA_csc.v:1285-1332](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L1285-L1332) — 例化 `NV_NVDLA_CSC_sg u_sg`，注意它同时输出 `sg2dl_pd`/`sg2wl_pd` 两路描述符，并接收 `accu2sc_credit_*` 反压、回送 `dp2reg_done`。

[NV_NVDLA_csc.v:1337-1635](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L1337-L1635) — 例化 `NV_NVDLA_CSC_wl u_wl`，驱动 `sc2buf_wt_rd`/`sc2buf_wmb_rd` 与 `sc2mac_wt_a/b_*`。

[NV_NVDLA_csc.v:1640-1946](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L1640-L1946) — 例化 `NV_NVDLA_CSC_dl u_dl`，驱动 `sc2buf_dat_rd` 与 `sc2mac_dat_a/b_*`，并接收独立的 `nvdla_wg_clk`（Winograd 门控时钟）。

四个 slcg 实例：

[NV_NVDLA_csc.v:1952-1997](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L1952-L1997) — `u_slcg_op_0/1/2` 分别给 sg/wl/dl 供门控时钟（`slcg_en_src_1` 恒为 1，仅由 `slcg_op_en` 控制）；`u_slcg_wg` 给 Winograd 路径供钟，其 `slcg_en_src_1 = slcg_wg_en`，即只有运行且处于 Winograd 模式才开钟。

跨片连接关系（承接 u3-l1）：CSC 的 `sc2mac_dat_a/b` 与 `sc2mac_wt_a/b` 在 `partition_c` 边界以 `sc2mac_dat_a_src_*` 等名字露出（见 [NV_NVDLA_partition_c.v:80-112](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v#L80-L112)），再由顶层 `NV_nvdla.v` 跨分区接到 `partition_m` 的 CMAC。

#### 4.1.4 代码实践

**实践目标**：建立 CSC 顶层「端口→子模块→下游」的连线直觉。

**操作步骤**：

1. 打开 [NV_NVDLA_csc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v)，定位 1282 行 `u_sg` 实例。
2. 在 `u_sg` 的端口连接里，找到 `sg2dl_pd`、`sg2wl_pd`、`sc_state`、`dp2reg_done`、`accu2sc_credit_vld` 这 5 个信号分别连到顶层哪根 wire（提示：均在 1218–1226 行的 wire 声明区）。
3. 追踪 `sg2dl_pd` 这根 wire 同时被 `u_sg`（输出）和 `u_dl`（输入，1643 行）引用；`sg2wl_pd` 被 `u_wl`（输入，1341 行）引用。

**需要观察的现象**：`sg2dl_pd`/`sg2wl_pd` 是「一源多宿」的描述符总线——sg 是唯一生产者，dl 与 wl 是消费者；`sc_state` 与 `dp2reg_done` 则由 sg 产出后被 regfile/wl/dl 共享。

**预期结果**：你能画出「sg 同时驱动 dl 与 wl」的扇出结构，并确认 regfile 不直接参与数据通路、只提供配置。

#### 4.1.5 小练习与答案

**练习 1**：CSC 顶层为什么要把 `sc_state`（sg 的状态）单独拉一根线给 wl 和 dl？
**答案**：wl/dl 需要知道 sg 当前处于 IDLE/PEND/BUSY/DONE 的哪个阶段，以便在 BUSY 时才真正发起 CBUF 读、在 DONE 时停止并排空，避免在 sg 未就绪时盲目取数。

**练习 2**：`sc2buf_wmb_rd_addr` 是 8 位，而 `sc2buf_dat_rd_addr`/`sc2buf_wt_rd_addr` 是 12 位，为什么 wmb 地址更窄？
**答案**：wmb（权重掩码位图）只存放在 CBUF 的 bank 15（u3-l3），容量远小于数据/权重所在的多 bank 空间，故所需地址位宽更小。

---

### 4.2 SG：slot 序列发生器（节拍源与反压）

#### 4.2.1 概念说明

SG（Sequence Generator）是 CSC 的「指挥」。它不直接搬数据，而是产出**一串 slot 描述符**：每个 slot 描述「这一拍，dl 该从 CBUF 哪里读多大一块数据、wl 该读哪一段权重」。SG 还承担两件大事：

- **迭代控制**：按 `atomics`（沿输出宽度的原子数）、`batches`（批数）、通道、kernel 等维度嵌套循环，遍历整层卷积。
- **反压协调**：用 CACC 回送的 `accu2sc_credit` 决定何时能生成下一拍数据，防止累加器溢出。

#### 4.2.2 核心流程

SG 的主状态机有四个状态（编码见源码精读）：

```
IDLE ──op_en & need_pending──▶ PEND ──pending_done──▶ BUSY
IDLE ──op_en (无需等待)──────▶ BUSY
BUSY ──layer_done & fifo排空 & 无在途包──▶ DONE
DONE ──dp2reg_done──▶ IDLE
```

- **IDLE**：空闲。`reg2dp_op_en`（CPU 写 OP_ENABLE 后，经 regfile 延迟 3 拍生效）拉高即启动。
- **PEND**：需要先等待（如信用不足或 CDMA 数据未就绪）。
- **BUSY**：持续生成 slot，分别压入 `SG_dat_fifo` 与 `SG_wt_fifo`。
- **DONE**：一层跑完，FIFO 排空后拉 `dp2reg_done`，触发 regfile 翻转 consumer、清 op_en。

反压的数学含义：累加器容量有限，SG 用一个 credit 计数器 `credit_cnt`（复位初值 256）跟踪可用额度。只有当

\[
\text{credit\_ready} = \neg\,\text{channel\_end} \,\lor\, (\text{credit\_cnt} \ge \text{credit\_req\_size})
\]

成立时，才允许生成一条「通道结束」的数据节拍。CACC 每腾出一格累加空间就发一次 `accu2sc_credit_vld` + `credit_size` 给 SG 加额度；SG 每发出一条 channel_end 数据拍就扣减 `dat_impact_cnt`。这是 u3-l1 提到的「`accu2sc_credit` 防累加器溢出」反压回路的具体实现。

#### 4.2.3 源码精读

状态机定义与编码：

[NV_NVDLA_CSC_sg.v:407-410](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L407-L410) — 四状态 localparam：`IDLE=00 / PEND=01 / BUSY=10 / DONE=11`。

[NV_NVDLA_CSC_sg.v:2326-2335](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L2326-L2335) — `sc_state` 输出：0=IDLE、1=PENDING、2=RUNNING(BUSY)、3=DONE，供 wl/dl 同步。

完成信号 `dp2reg_done`（触发影偶切换）：

[NV_NVDLA_CSC_sg.v:2385-2391](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L2385-L2391) — `dp2reg_done <= is_done && (sg_dn_cnt == 6'b1);`，即进入 DONE 且排空计数到 1 时拉一拍 done 脉冲。

**slot 数据包 `sg2dl_pd` 的字段组装**（这是「一个 slot 内数据如何被组装」的核心）：

[NV_NVDLA_CSC_sg.v:6823-6833](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L6823-L6833) — 31 位数据描述符打包：

| 位域 | 字段 | 含义 |
|------|------|------|
| [4:0] | w_offset | 当前 slot 在输出宽度方向的偏移 |
| [9:5] | h_offset | 高度方向偏移 |
| [16:10] | channel_size | 本 slot 涉及的输入通道长度 |
| [23:17] | stripe_length | 条带长度 |
| [25:24] | cur_sub_h | 当前子高度分段 |
| [26] | block_end | 块结束 |
| [27] | channel_end | 通道结束（C 维累加完，对应一次 credit 扣减） |
| [28] | group_end | 组结束 |
| [29] | layer_end | 层结束 |
| [30] | dat_release | 数据释放/复用控制 |

**slot 权重包 `sg2wl_pd` 的字段组装**：

[NV_NVDLA_CSC_sg.v:7090-7103](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L7090-L7103) — 18 位权重描述符打包：

| 位域 | 字段 | 含义 |
|------|------|------|
| [6:0] | weight_size | 本 slot 权重长度 |
| [12:7] | kernel_size | 输出 kernel 数 |
| [14:13] | cur_sub_h | 当前子高度分段 |
| [15] | channel_end | 通道结束 |
| [16] | group_end | 组结束 |
| [17] | wt_release | 权重释放/复用控制 |

两路描述符分别经一对 FIFO 解耦时序：

[NV_NVDLA_CSC_sg.v:7109-7133](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L7109-L7133) — 例化 `NV_NVDLA_CSC_SG_dat_fifo u_dat_fifo`（33 位：32 位描述符+1 位 pkg_idx）与 `NV_NVDLA_CSC_SG_wt_fifo u_wt_fifo`（20 位）。FIFO 让 sg 可以提前生成若干 slot，而 dl/wl 按自己的节拍消费。

credit 反压计数器：

[NV_NVDLA_CSC_sg.v:8073-8102](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L8073-L8102) — `credit_cnt` 复位为 `9'h100`（256），按 `credit_cnt + add - dec` 更新；`credit_ready = ~sg2dat_channel_end | (credit_cnt >= credit_req_size)`。

[NV_NVDLA_CSC_sg.v:8020-8036](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L8020-L8036) — `accu2sc_credit_vld`/`accu2sc_credit_size` 打一拍寄存后作为 `credit_vld`/`credit_size`，用于 credit 增量。

Winograd 模式下原子数被除以 4（一次 Winograd 变换产出 2×2=4 个输出点）：

[NV_NVDLA_CSC_sg.v:2853-2862](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L2853-L2862) — `data_out_atomic_w` 在 `is_winograd` 时取 `{2'b0, reg2dp_atomics[20:2]} + 1`（即 atomics/4 + 1），普通卷积取 `atomics + 1`。

#### 4.2.4 代码实践

**实践目标**：把一个 slot 的数据包与权重包「拆开」，理解 sg 同时给 dl、wl 下发的两个描述符各包含什么。

**操作步骤**：

1. 打开 [NV_NVDLA_CSC_sg.v:6823-6833](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L6823-L6833) 与 [7090-7103](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L7090-L7103)。
2. 假设某拍 sg 产出 `sg2dl_pd = 31'b...` 且 `channel_end=1`、`layer_end=0`、`dat_release=0`；同时产出 `sg2wl_pd` 且 `wt_release=1`。请在两个表格中分别标出这些位，并用一句话说明 dl 与 wl 各自该做什么。
3. 对照 [7109-7133](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L7109-L7133) 的两个 FIFO，说明为何数据描述符宽度（33）大于权重描述符宽度（20）。

**需要观察的现象**：数据包比权重包多了 `w_offset/h_offset/channel_size/stripe_length/block_end/layer_end` 等字段——因为数据要按卷积窗口在二维空间滑动并处理 padding，而权重只需按 kernel 维度顺序读出。

**预期结果**：你能解释「同一条 slot，dl 拿到的是空间坐标+通道长度，wl 拿到的是权重长度+kernel 数」；二者通过 `channel_end`/`group_end` 等结束标志保持同步。**（具体仿真波形待本地验证）**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `credit_cnt` 复位初值是 256 而不是 0？
**答案**：复位时 CACC 还未发任何 credit，但 SG 需要一个初始额度才能开始生成首批 channel_end 数据拍；256 对应累加器初始可容纳的拍数上限，避免 SG 启动即被反压卡死。

**练习 2**：`sg2dl_pd` 与 `sg2wl_pd` 都带 `channel_end`，为什么数据包还多一个 `block_end`/`layer_end` 而权重包没有 `layer_end`？
**答案**：数据通路需要更细的层级边界（block/channel/group/layer）来驱动 padding 注入与 CBUF 释放；权重在层结束时随最后一次 channel_end 释放即可，不需要单独的 layer_end 标志。

---

### 4.3 WL 与 DL：权重/数据加载器与 cmac_a/cmac_b 分流

#### 4.3.1 概念说明

DL（Data Loader）与 WL（Weight Loader）是两个对称的「搬运工」：

- **DL** 消费 `sg2dl_pd`，从 CBUF 的 dat 读口取 1024 位数据，做 padding 注入、精度重排（INT8/INT16/FP16）、可选 Winograd 变换，最终每拍输出 **128 字节**特征数据。
- **WL** 消费 `sg2wl_pd`，从 CBUF 的 wt 读口取 1024 位权重、从 wmb 读口取权重掩码位图，经 `WL_dec` 解码与掩码，最终每拍输出 **128 字节**权重。

二者最关键的差异在于**对 cmac_a / cmac_b 两半阵列的喂法**：

- **数据广播**：DL 把同一份 128 字节特征数据**同时**送给 `cmac_a` 与 `cmac_b`——两半阵列看到的是同一块输入特征图。
- **权重分流**：WL 用一个 16 位 `sel` 信号，低 8 位选 `cmac_a`、高 8 位选 `cmac_b`，把**不同**的权重分别送给两半。

这对应一个清晰的架构意图：**同一块输入特征，配两套不同权重，两半 MAC 阵列并行算两个输出通道组（K 维）**。这就是 CMAC 分 a/b 两半、每半 1024 MAC 能在同一拍处理两组输出 kernel 的根因。

#### 4.3.2 核心流程

DL 的数据流（Winograd 关闭时）：

```
sg2dl_pd → 解包(w_offset/h_offset/channel_size/stripe_length…) 
         → 计算本拍应读的 CBUF bank/addr（含 pad_left/pad_top 偏移）
         → sc2buf_dat_rd_en/addr 发读请求
         → sc2buf_dat_rd_data(1024b) 返回
         → 按卷积窗口重排 + padding 注入(pad_value)
         → dat_out_data(512b bypass) → 复制成 1024b
         → 广播到 sc2mac_dat_a/b (各 128×8b=1024b) + mask[127:0] + pvld
```

WL 的数据流：

```
sg2wl_pd → 解包(weight_size/kernel_size…) 
         → 计算本拍应读的 CBUF wt/wmb 地址
         → sc2buf_wt_rd / sc2buf_wmb_rd 发读请求
         → sc2buf_wt_rd_data(1024b) + sc2buf_wmb_rd_data(1024b) 返回
         → WL_dec：按精度(int8/int16/fp16)与 wmb 掩码解码权重
         → sc2mac_out_sel[15:0]：[7:0]→cmac_a，[15:8]→cmac_b
         → sc2mac_wt_a/b (各 128×8b=1024b) + mask[127:0] + sel[7:0] + pvld
```

padding 的注入原理：DL 维护输入坐标计数器 `datain_w_cnt`/`datain_h_cnt`，其起点设为「负的 pad」：

\[
\text{datain\_w\_cnt\_st} = 0 - \text{pad\_left},\quad \text{datain\_h\_cnt\_st} = 0 - \text{pad\_top}
\]

当坐标落在负区间（即 padding 区）时，该数据槽填 `pad_value` 而非 CBUF 读出值。

#### 4.3.3 源码精读

**WL 解包 `sg2wl_pd`**：

[NV_NVDLA_CSC_wl.v:3933-3939](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_wl.v#L3933-L3939) — 把 18 位 `wl_pd` 拆成 `wl_weight_size[6:0]`、`wl_kernel_size[5:0]`、`wl_cur_sub_h[1:0]`、`wl_channel_end`、`wl_group_end`、`wl_wt_release`，与 sg 的打包逐位对应。

**WL 的 a/b 分流（核心！）**：

[NV_NVDLA_CSC_wl.v:10439-10453](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_wl.v#L10439-L10453) — 解码器输出 `sc2mac_out_sel[15:0]`，低 8 位与 pvld 相与得 `sc2mac_out_a_sel_w`（送 cmac_a），高 8 位得 `sc2mac_out_b_sel_w`（送 cmac_b）；`sc2mac_wt_a_pvld = |a_sel_w`、`sc2mac_wt_b_pvld = |b_sel_w`。即**权重按 sel 分别送往 cmac_a 或 cmac_b**。

**wmb 掩码提取**（压缩权重的稀疏标记）：

[NV_NVDLA_CSC_wl.v:6312-6321](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_wl.v#L6312-L6321) — INT8 时直接用 `sc2buf_wmb_rd_data[127:0]`；INT16/FP16 时把每个 wmb 位复制 2 份展开；非压缩权重（`~is_compressed`）时掩码全 1（全部有效）。

**DL 的数据广播（核心！）**：

[NV_NVDLA_CSC_dl.v:20334-20347](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L20334-L20347) — `sc2mac_dat_a_pvld` 与 `sc2mac_dat_b_pvld` 都赋为同一个 `dl_out_pvld`，且两路 `data0..127` 都来自同一组 `dl_out_data0..127`。即**同一份特征数据广播给 cmac_a 与 cmac_b**。

**DL 的 padding 注入**：

[NV_NVDLA_CSC_dl.v:9772-9779](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L9772-L9779) — 宽度计数起点：普通卷积为 `0 - reg2dp_pad_left`（用负值表示落入左 padding 区），Winograd 为 `14'h2`，IMG 模式为 0。

[NV_NVDLA_CSC_dl.v:16224-16279](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L16224-L16279) — 当某数据槽处于 padding 区（`dat_l0c0_dummy` 等为真）时，用 `dat_rsp_pad_value` 替换 CBUF 读出值。

[NV_NVDLA_CSC_dl.v:16228](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L16228) — pad 值扩展：INT8 时 `{64{pad_value[7:0]}}`（64 个 8 位），INT16 时 `{32{pad_value}}`。

**DL 数据出口 mux（bypass vs Winograd）**：

[NV_NVDLA_CSC_dl.v:18883-18885](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L18883-L18885) — `dat_out_data = is_winograd ? dat_out_wg_data : is_int8 ? {2{bypass[511:0]}} : bypass`。普通卷积走 bypass 路径（512 位复制成 1024 位）；Winograd 走变换后数据。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：对照 `NV_NVDLA_CSC_sg.v` 与 `wl`/`dl`，说明一个 slot 内的数据与权重如何被组装，并指出它们送往 `cmac_a` 还是 `cmac_b`。

**操作步骤**：

1. 在 [NV_NVDLA_CSC_sg.v:6823-6833](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L6823-L6833) 取一个数据 slot，写出其 31 位字段布局；在 [7090-7103](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L7090-L7103) 取对应权重 slot，写出 18 位字段布局。
2. 追数据去向：打开 [NV_NVDLA_CSC_dl.v:20334-20347](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L20334-L20347)，确认数据 `dl_out_data` 同时驱动 `sc2mac_dat_a_*` 与 `sc2mac_dat_b_*` → **数据广播到 cmac_a 与 cmac_b**。
3. 追权重去向：打开 [NV_NVDLA_CSC_wl.v:10439-10453](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_wl.v#L10439-L10453)，确认 `sc2mac_out_sel[7:0]`→`cmac_a`、`[15:8]`→`cmac_b` → **权重按 sel 分流到 cmac_a 或 cmac_b**。
4. 用一句话总结：同一拍，cmac_a 与 cmac_b 拿到的特征数据是否相同？权重是否相同？

**需要观察的现象**：数据通路 `sc2mac_dat_a_pvld` 与 `sc2mac_dat_b_pvld` 同源、数据同值；权重通路 `sc2mac_wt_a_pvld` 与 `sc2mac_wt_b_pvld` 由不同 sel 位驱动、可独立有效。

**预期结果**：结论是「**数据相同（广播），权重不同（分流）**」——cmac_a 与 cmac_b 用同一块输入特征、配各自的权重，并行计算两个输出通道组。若用 VCS/Verilator 跑 sanity trace 并在 `sc2mac_dat_a_data0` 与 `sc2mac_dat_b_data0` 上设断点，应观察到二者同拍同值；而在 `sc2mac_wt_a_*` 与 `sc2mac_wt_b_*` 上观察到不同权重。**（波形待本地验证）**

#### 4.3.5 小练习与答案

**练习 1**：既然数据广播给两半、权重分流，那 CMAC 两半阵列一拍到底算了几个输出通道？
**答案**：两半各配一套权重，故一拍并行处理两组输出 kernel（K 维方向的两个原子组）。数据（C 维 reduction）则由多拍累加在 CACC 完成。

**练习 2**：`sc2mac_wt_a_sel`/`sc2mac_wt_b_sel` 是 8 位，而 mask 是 128 位，二者关系是什么？
**答案**：`sel[7:0]` 是「8 个 MAC 子组的有效选择」（粗粒度，决定 pvld 是否拉起）；`mask[127:0]` 是「128 个字节级有效位」（细粒度，标记稀疏/压缩权重的逐字节有效性，配合 wmb）。

**练习 3**：DL 的 `datain_w_cnt_st = 0 - pad_left` 用减法表示负坐标，为什么不直接从 0 开始计数？
**答案**：卷积窗口在输入图左/上边缘会「悬空」到 padding 区。让计数器从 `-pad_left` 起算，则计数器 `<0` 的拍即为 padding 拍，自然触发 `dummy→pad_value` 替换，无需单独的边界状态机。

---

### 4.4 Winograd 变换支持

#### 4.4.1 概念说明

Winograd 是一种用「少量乘法 + 多量加法」实现卷积的算法，对 3×3 stride-1 卷积可把乘法数降到约原来的 4/9，在 INT8 下显著提升吞吐。代价是输入数据要先做一次线性变换（矩阵加法），输出端再做一次变换（在 CACC/CMAC 侧）。

NVDLA 把**输入端变换**放在 CSC 的 DL 里实现，关键设计有三点：

1. **模式选择**：`reg2dp_conv_mode`（0=直接卷积，1=Winograd）决定 DL 走 bypass 还是变换路径。
2. **独立时钟域**：变换逻辑用单独的门控时钟 `nvdla_wg_clk`，仅当「运行中且为 Winograd 模式」时才开钟，直接卷积时完全关钟省电。
3. **pra_cell 预加单元**：4 个 `pra_cell` 实例在 `nvdla_wg_clk` 下做输入数据的加法变换（Winograd 的 Aᵀ·d·A 由加法网络实现），并支持 `pra_truncate` 控制变换中间值的截断精度。

#### 4.4.2 核心流程

```
reg2dp_conv_mode == 1 (Winograd)
   │
   ├──▶ is_winograd = 1
   │
   ├──▶ slcg_wg_en = reg2dp_op_en & is_winograd   （门控开钟条件）
   │        └─ 3 拍延迟 ─▶ slcg_wg_en ─▶ u_slcg_wg ─▶ nvdla_wg_clk
   │
   ├──▶ SG 侧：data_out_atomic = atomics/4 + 1   （4 个输出点合一）
   │
   └──▶ DL 侧：
          dat_rsp_wg_ch[0..3] ──▶ u_pra_cell_0..3 (nvdla_wg_clk)
                                  ├─ cfg_precision / cfg_truncate
                                  └─▶ dat_pra_dat_ch[0..3] (各 256b)
                                          │
                          重排 ─▶ dat_out_wg_8b / _16b ─▶ dat_out_wg_data
                                          │
          dat_out_data = is_winograd ? dat_out_wg_data : bypass_data
```

Winograd 把 4 个输出点合并处理，因此 SG 的原子数被除以 4（见 4.2.3 的 `data_out_atomic_w`）。pra_cell 的截断 `pra_truncate` 取值 0–2（编码 3 会被映射回 2），用于在加法网络中丢弃低位、控制中间精度与面积。

#### 4.4.3 源码精读

模式选择：

[NV_NVDLA_CSC_dl.v:1948-1958](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L1948-L1958) — `is_winograd = (reg2dp_conv_mode == 1'h1)`、`is_conv = (reg2dp_conv_mode == 1'h0)`。

Winograd 门控使能：

[NV_NVDLA_CSC_dl.v:6687](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L6687) — `assign slcg_wg_en_w = reg2dp_op_en & is_winograd;`，再经 3 拍流水线（[6689-6711](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L6689-L6711)）得 `slcg_wg_en`，回送给顶层的 `u_slcg_wg`（见 4.1.3）。

pra_cell 实例（4 个，均在 `nvdla_wg_clk` 下）：

[NV_NVDLA_CSC_dl.v:18636-18651](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L18636-L18651) — `NV_NVDLA_CSC_pra_cell u_pra_cell_0`，时钟接 `nvdla_wg_clk`，输入 `dat_rsp_wg_ch0_d1[255:0]`，输出 `dat_pra_dat_ch0[255:0]`，受 `pra_precision_0`/`pra_truncate_0` 控制。共有 u_pra_cell_0..3 四个实例（[18654/18672/18690](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L18654-L18705)），对应 4 路变换。

Winograd 数据出口选择：

[NV_NVDLA_CSC_dl.v:18883-18885](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L18883-L18885) — Winograd 时选 `dat_out_wg_data`（变换后），否则 INT8 时把 512 位 bypass 复制成 1024 位、其余直接 bypass。

pra_truncate 的限幅：

[NV_NVDLA_CSC_dl.v:2227-2232](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L2227-L2232) — `pra_truncate_w = (reg2dp_pra_truncate == 2'h3) ? 2'h2 : reg2dp_pra_truncate`，把编码 3 映射回 2，再广播给 4 个 pra_cell。

#### 4.4.4 代码实践

**实践目标**：理解 Winograd 路径的「按需开钟」与数据出口切换。

**操作步骤**：

1. 在 [NV_NVDLA_CSC_dl.v:6687](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L6687) 确认 `slcg_wg_en` 仅在 `op_en & is_winograd` 时为真。
2. 回到顶层 [NV_NVDLA_csc.v:1988-1997](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_csc.v#L1988-L1997) 的 `u_slcg_wg`，确认 `slcg_en_src_1 = slcg_wg_en`，而其它三个 slcg 的 `slcg_en_src_1 = 1'b1`。
3. 在 [18883-18885](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L18883-L18885) 确认 `dat_out_data` 在 Winograd 与直接卷积间的切换。

**需要观察的现象**：直接卷积模式下，`nvdla_wg_clk` 被门控关断，4 个 pra_cell 不翻转，零动态功耗；切到 Winograd 模式后该时钟才启用。

**预期结果**：你能预测「跑一个 3×3 stride-1 Winograd 层时 `nvdla_wg_clk` 有翻转、跑 1×1 或直接卷积时无翻转」。**（功耗/波形待本地验证，可用 `+icg_summary` 选项观察 ICG 关钟比例，见 [NV_NVDLA_CSC_slcg.v:392-416](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_slcg.v#L392-L416)）**

#### 4.4.5 小练习与答案

**练习 1**：为什么 Winograd 的门控时钟 `nvdla_wg_clk` 要单独设，而不直接用 DL 的 `nvdla_op_gated_clk_2`？
**答案**：pra_cell 只有在 Winograd 模式才需要工作。用单独的门控时钟，可以在直接卷积时彻底关断 pra_cell 的时钟，比让它们空转更省电；DL 的其余逻辑（取数、padding、bypass）在两种模式下都要工作，故用 `nvdla_op_gated_clk_2`。

**练习 2**：SG 在 Winograd 模式下把 `atomics` 除以 4（`atomics[20:2]`），这与 pra_cell 数量为 4 有关系吗？
**答案**：有关。一次 Winograd 变换从 4×4 输入_tile 产出 2×2=4 个输出点，4 个 pra_cell 正好对应这 4 路并行的输入变换；因此「4 个输出点合一」使得沿输出宽度的原子数缩为原来的 1/4。

---

## 5. 综合实践

**任务**：以「一拍卷积数据从 CBUF 走到 CMAC」为主线，把本讲四个模块串起来，画出一张完整的 CSC 节拍时序图，并预测一个带 padding 的 3×3 卷积层的 slot 序列特征。

**操作步骤**：

1. **配置侧**（承接 u2-l3）：阅读 [NV_NVDLA_CSC_regfile.v:486-543](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_regfile.v#L486-L543)，说明 CPU 写 OP_ENABLE（[NV_NVDLA_CSC_dual_reg.v:206,243](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dual_reg.v#L206-L243)）后，`op_en` 如何经 3 拍延迟变成 `reg2dp_op_en` 去启动 SG，并经 [545-573](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_regfile.v#L545-L573) 的 `slcg_op_en` 再延迟 3 拍去开各 slcg 的钟。
2. **节拍生成**：追踪 SG 从 IDLE→BUSY，生成一条 `sg2dl_pd`（含 `channel_end`）与对应 `sg2wl_pd`，分别入 `SG_dat_fifo`/`SG_wt_fifo`（[7109-7133](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L7109-L7133)）。
3. **反压点**：在 [8073-8102](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_sg.v#L8073-L8102) 标出 credit 不足时 SG 会停在哪一步。
4. **数据侧**：DL 消费 slot → 发 `sc2buf_dat_rd` → 收 1024 位 → padding 注入（[16224-16279](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L16224-L16279)）→ 广播到 `sc2mac_dat_a/b`（[20334-20347](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_dl.v#L20334-L20347)）。
5. **权重侧**：WL 消费 slot → 发 `sc2buf_wt_rd`/`sc2buf_wmb_rd` → WL_dec 解码 → 按 sel 分流到 `sc2mac_wt_a/b`（[10439-10453](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/csc/NV_NVDLA_CSC_wl.v#L10439-L10453)）。
6. **预测**：对一个 `pad=1` 的 3×3 卷积，输出图最左/最上一列对应的 slot 中，DL 的 `datain_w_cnt`/`datain_h_cnt` 会取到负值，此时 `dat_*_dummy` 为真、数据被 `pad_value` 替换；这些 padding 拍仍照常向 CMAC 广播，由 mask 标记有效性。

**预期产出**：一张标注了 `op_en→slcg开钟→SG生成slot→DL/WL取CBUF→cmac_a/b 收数` 的时序图，以及一段说明「数据广播、权重分流、padding 注入、credit 反压」如何在同一拍协同的文字。**完整波形待本地用 sanity trace + DUMP=1 DUMPER=VERDI 验证（见 u1-l4）。**

## 6. 本讲小结

- CSC 是卷积主流水线第三级，职责是把 CBUF 里的特征图与权重「重排 + 节拍对齐」后喂给 CMAC；顶层由 `regfile + sg + wl + dl + 4×slcg` 拼成。
- **SG 是指挥**：用四状态机（IDLE/PEND/BUSY/DONE）生成 slot 描述符 `sg2dl_pd`(31b)/`sg2wl_pd`(18b)，经两个 FIFO 下发，并用 CACC 的 `accu2sc_credit` 做 credit 反压防溢出。
- **DL 与 WL 是搬运工**：DL 读 dat 并广播同一份 128 字节特征到 `cmac_a`/`cmac_b`；WL 读 wt/wmb 并按 `sel[15:0]`（低 8 位 a、高 8 位 b）分流不同权重到两半——故两半阵列「同数据、异权重」，并行算两个输出通道组。
- **padding 在 DL 注入**：输入坐标计数器从 `-pad_left/-pad_top` 起算，负坐标拍用 `pad_value` 替换 CBUF 读出值。
- **Winograd 在 DL 实现**：`conv_mode=1` 时走 pra_cell 变换路径，用独立门控时钟 `nvdla_wg_clk`（`op_en & is_winograd` 才开钟），SG 侧原子数除以 4。
- **影偶配置闭环**：SG 跑完一层拉 `dp2reg_done`，regfile 据此翻转 consumer、清 op_en，引擎无缝接跑下一层（承接 u2-l3）。

## 7. 下一步学习建议

- 下一篇 **u3-l5 CMAC：乘加阵列与定点/浮点计算** 会接住本讲 `sc2mac_dat_a/b` 与 `sc2mac_wt_a/b`，讲解 CMAC 如何用 128×8 位数据与权重做并行乘加、产生 partial sum。重点关注 `cmac_a`/`cmac_b` 如何消费本讲的「同数据异权重」。
- 想深入反压机制，可重读本讲 SG 的 credit 计数器，再到 **u3-l6 CACC** 看 `accu2sc_credit` 的产生端，形成完整闭环。
- 想理解 padding/stride/dilation 的几何含义如何映射到地址生成，可对照 `cmod`（C 参考模型，见 u7-l3）中 CSC 对应的 C++ 实现，RTL 与 C 模型一一对应。
- 若关注低功耗，可结合 **u6-l1 时钟域、复位与时钟门控** 重新审视本讲的 4 个 slcg 与 `slcg_wg_en` 的 3 拍延迟设计。
