# axi_xp 交叉点与 interleaved_xbar

## 1. 本讲目标

本讲在标准 `axi_xbar`（u6-l1）之上，介绍两个「高级互连」变体。学完后读者应能：

- 说清 `axi_xp`（Crosspoint）为什么要求 slave 与 master 端口**同构（homomorphous，即两侧 ID 等宽）**，以及它是如何用 `axi_xbar + axi_id_remap` 把内部被加宽的 ID 再压回去的。
- 解释 `axi_interleaved_xbar` 的**地址交错（interleaving）**策略：用地址中的一段 bank 位直接选 master 端口，并把这段位从送往下游的地址里抹掉。
- 指出 `axi_interleaved_xbar` 的「实验性」警告来自源码与 README 的哪一处，并理解它为什么没有 testbench。
- 在 `axi_xbar`、`axi_xp`、`axi_interleaved_xbar` 三者之间为一个具体拓扑做选型判断。

## 2. 前置知识

本讲直接承接 u6-l1（xbar 架构与配置）。需要先牢固掌握两点：

- **xbar 的 ID 宽度不对称**：标准 `axi_xbar` 的 master 端口 ID 比 slave 端口宽 \( \lceil\log_2(\text{NoSlvPorts})\rceil \) 位，多出的高位是 mux 用 `axi_id_prepend` 前置的「来源 slave 端口号」标签，用于把 B/R 响应路由回正确的源（见 u5-l3、u6-l1）。这意味着**每经过一级 xbar，ID 就被加宽一次**——级联两三级之后下游端口就要支持极宽的 ID，这在深层片上网络里不可接受。
- **`axi_id_remap`**：把宽 ID 重映射为窄 ID 的模块，内部维护一张 ID 映射表，受 `AxiSlvPortMaxUniqIds`（同时在途不同 ID 数上限）约束（见 u10-l1）。它是「把 ID 压回去」的标准工具。

另外回顾两种路由思路，本讲会反复对比：

- **地址区间路由**：按地址落在哪段区间选 master 端口（`addr_decode`，xbar 的做法）。
- **地址交错路由**：按地址中的某几位（bank 位）选 master 端口，常用于存储 bank 交错以提升带宽。

## 3. 本讲源码地图

| 文件 | 角色 | Bender 编译层级 |
|---|---|---|
| [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv) | 标准全连接 crossbar，本讲作为**对照基准** | Level 5 |
| [src/axi_xp.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv) | 同构端口交叉点 = 1 个 `axi_xbar` + 每 master 端口 1 个 `axi_id_remap` | **Level 6（全库顶层）** |
| [src/axi_interleaved_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv) | 可在「地址区间 / 地址交错」两种路由间运行期切换的**实验性** crossbar | Level 4 |

> 说明：编译层级取自 `Bender.yml`。注意 `axi_interleaved_xbar`（Level 4）其实**低于** `axi_xbar`（Level 5），它并不复用 `axi_xbar_unmuxed`，而是自己直接拼 `addr_decode + axi_demux + axi_mux + axi_err_slv`；而 `axi_xp`（Level 6）站在最顶端，直接例化 `axi_xbar`。

## 4. 核心概念与源码讲解

### 4.1 axi_xp：同构（homomorphous）端口的交叉点

#### 4.1.1 概念说明

`axi_xp` 的模块注释一行就说清了它的设计意图：

> AXI Crosspoint (XP) with homomorphous slave and master ports.

「homomorphous（同构）」指的是 **slave 端口和 master 端口使用完全相同的 ID 宽度**（同一个参数 `AxiIdWidth`），不像 `axi_xbar` 那样 master 侧更宽。这一点从端口参数就能看出——`axi_xp` 只有一个 `AxiIdWidth` 同时管两侧（[src/axi_xp.sv:34-40](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv#L34-L40)），而 `axi_xbar` 的两侧 ID 宽度由 `Cfg.AxiIdWidthSlvPorts` 经公式推导出更宽的 master 侧。

为什么要追求同构？**为了能把交叉点像瓦片一样平铺、级联，构造规则的网络（如 mesh、fat-tree）**。如果每一跳都加宽 ID，三级之后 ID 宽度就失控；同构端口让每一级的输入输出宽度一致，可以无限拼接。代价是：要把内部被加宽的 ID 重新压回原宽度，必须在每个 master 端口挂一个 `axi_id_remap`，其容量（同时在途不同 ID 数）成为新的并发瓶颈。

#### 4.1.2 核心流程

`axi_xp` 的实现极简，本质是「**借 xbar 干活，再用 id_remap 把 ID 压回去**」：

```
外部 slave 端口 (窄 id_t)
        │  axi_req_t / axi_resp_t
        ▼
   ┌──────────────────────────────────────┐
   │  i_xbar : axi_xbar                    │
   │    slave 侧用窄 xp_*  类型            │
   │    master 侧用宽 xbar_* 类型          │   ← 内部 master ID 被加宽
   └──────────────────────────────────────┘
        │  xbar_req_t / xbar_resp_t (宽 xbar_id_t)
        ▼  每个 master 端口各一个
   ┌──────────────────────────────────────┐
   │  i_axi_id_remap : 宽 → 窄             │
   │    slv 侧 = xbar_req_t (宽)           │
   │    mst 侧 = axi_req_t  (窄)           │   ← ID 被压回 AxiIdWidth
   └──────────────────────────────────────┘
        │  axi_req_t / axi_resp_t
        ▼
外部 master 端口 (窄 id_t)  ← 与 slave 端口同构
```

关键的宽度关系（直接来自源码常量）：

\[
\text{AxiXbarIdWidth} = \text{AxiIdWidth} + \lceil\log_2(\text{NumSlvPorts})\rceil
\]

这正是 `axi_xbar` 内部 master 端口的 ID 宽度；`axi_id_remap` 再把它映射回 `AxiIdWidth`。

#### 4.1.3 源码精读

**(1) 两套类型——窄 `xp_*` 与宽 `xbar_*`。** 模块先用 `AXI_TYPEDEF_ALL` 生成两套通道/请求/响应类型：一套用窄 `id_t`（外部端口），一套用宽 `xbar_id_t`（xbar 内部 master 侧）：

[src/axi_xp.sv:102-115](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv#L102-L115) 定义了 `AxiXbarIdWidth`、两套 `id_t`，并用宏生成 `xp_*` / `xbar_*` 类型，再声明内部连线 `xbar_req/xbar_resp`。这一段是「同构外壳 + 异构内核」的关键：外壳两侧都是 `AxiIdWidth`，内核却允许 xbar 用更宽的 ID。

**(2) 借来的 xbar。** `axi_xp` 直接例化一个 `axi_xbar`，把窄类型接到 slave 侧、宽类型接到 master 侧（[src/axi_xp.sv:117-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv#L117-L146)）。注意两个细节：

- `mst_req_t`/`mst_resp_t` 填的是 `xbar_req_t`/`xbar_resp_t`（宽 ID），所以 xbar 输出的 `xbar_req` 仍带被加宽的 ID。
- `en_default_mst_port_i('0)`、`default_mst_port_i('0)` 被硬接为 0——**`axi_xp` 没有把 xbar 的「默认 master 端口」能力对外引出**，它的端口列表里根本没有这两个输入。这是它与 `axi_xbar` 的一个功能差异（xbar 有，见 [src/axi_xbar.sv:86-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L86-L90)）。

**(3) 每 master 端口一个 id_remap 把 ID 压回去。** 一个 `for` 循环为每个 master 端口例化一个 `axi_id_remap`（[src/axi_xp.sv:148-166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv#L148-L166)）。宽度映射是理解整模块的钥匙：

- `AxiSlvPortIdWidth = AxiXbarIdWidth`（id_remap 的输入侧 = xbar 的宽 master ID）；
- `AxiMstPortIdWidth = AxiIdWidth`（id_remap 的输出侧 = 外部窄 ID）；
- `AxiSlvPortMaxUniqIds` / `AxiMaxTxnsPerId` 限制同时在途的不同 ID 数与每 ID 事务数。

经过这一级，外部 master 端口的 `mst_req_o[i]` 就回到了窄 `axi_req_t`，与 slave 端口同宽——同构达成。

> ⚠️ **文档与实现的细微出入（读源码时注意）**：`axi_xp` 的参数注释（[src/axi_xp.sv:48-73](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv#L48-L73)）提到，当 `AxiSlvPortMaxUniqIds > 2**AxiMstPortIdWidth` 时应改走 `axi_id_serialize`，并列出了 `AxiSlvPortMaxTxns`、`AxiMstPortMaxUniqIds`、`AxiMstPortMaxTxnsPerId` 等参数。但在当前版本的实际例化（L148-166）里，**只接了 `axi_id_remap`，上述 serialize 相关参数并未连入任何子模块**。也就是说，这些参数当前是「占位 + 文档预告」，真正生效的只有喂给 `axi_id_remap` 的 `AxiSlvPortMaxUniqIds` 与 `AxiSlvPortMaxTxnsPerId`。读源码时请以例化代码为准。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（`axi_xp` 与 `axi_interleaved_xbar` 在 `test/` 下都没有 testbench，已通过 Glob 确认）。

1. **实践目标**：亲手验证「同构 = xbar + id_remap」这条数据通路，并把宽度关系对上。
2. **操作步骤**：
   - 打开 [src/axi_xp.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv)，定位 L102 的 `AxiXbarIdWidth` 表达式。
   - 跟着 L111-L112 的两套 `AXI_TYPEDEF_ALL`，列出 `xp_aw_chan_t` 与 `xbar_aw_chan_t` 各自 `id` 字段的位宽。
   - 在 L117-L146 的 `i_xbar` 例化里，确认 slave 侧用的是窄类型、master 侧用的是宽类型。
   - 在 L148-L166 的 `gen_remap` 里，确认 `i_axi_id_remap` 的 `AxiSlvPortIdWidth` 与 `AxiMstPortIdWidth` 分别填了哪个常量。
3. **需要观察的现象**：类型在「窄 → 宽（进 xbar）→ 窄（出 id_remap）」之间两次切换；外部 `slv_req_i` 与 `mst_req_o` 的元素类型同为 `axi_req_t`。
4. **预期结果**：你能画出本节 4.1.2 的那张数据流框图，并标注每一段的 ID 宽度。
5. **待本地验证**：若想确认可综合性，可尝试用 `make compile.log`（见 u1-l4）对一个仅例化 `axi_xp` 的顶层做 elaboration（本库未提供 `axi_xp` 的现成 TB，故仅做 elaborate 检查）。

#### 4.1.5 小练习与答案

**练习 1**：若 `NumSlvPorts = 4`、`AxiIdWidth = 8`，`axi_xp` 内部 `i_xbar` 的 master 端口 ID 宽度是多少？外部 master 端口呢？
**答案**：内部为 \( 8 + \lceil\log_2 4\rceil = 10 \) 位（`AxiXbarIdWidth`）；外部 master 端口经 `axi_id_remap` 压回 8 位，与 slave 端口同构。

**练习 2**：为什么 `axi_xp` 比 `axi_xbar` 更适合做「可级联的网络节点」？
**答案**：因为 `axi_xp` 两侧 ID 等宽，多个 xp 可以背靠背拼接而 ID 宽度不增长；`axi_xbar` 每级加宽，级联几级后下游端口无法承受。

**练习 3**：`axi_xp` 的端口列表里为什么没有 `en_default_mst_port_i`？
**答案**：它在内部把这两个信号硬接为 `'0`（L144-L145），即不对外暴露 xbar 的「默认 master 端口」能力，未映射地址只能落到 xbar 自带的译码错误从端（返回 DECERR）。

---

### 4.2 axi_interleaved_xbar：可切换的地址交错互连

#### 4.2.1 概念说明

`axi_interleaved_xbar` 是标准 xbar 的一个**变体**：它在「按地址区间路由」之外，多了一种「按地址中的 bank 位路由」的**交错（interleaved）模式**，并由输入 `interleaved_mode_ena_i` 在运行期切换。

最关键的一句警告写在模块正上方（[src/axi_interleaved_xbar.sv:18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L18)）：

> Interleaved version of the crossbar. This module is experimental; use at your own risk.

README 的模块表里也原样重复了这句（[README.md:43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L43)），并且**没有给它挂文档链接**。结合 `test/` 下没有对应 testbench 这一事实，可以把它的「实验性」理解为：**可用、但未经与 `axi_xbar` 同等规模的随机回归验证，使用者需自行兜底**。

#### 4.2.2 核心流程

每个 slave 端口都做一次「**译码 → 选择 → （可选）改地址 → demux**」，再过一个 cross 矩阵喂给每 master 端口的 mux。与 `axi_xbar` 的根本差异在「选择」与「改地址」两步：

```
                  ┌── interleaved_mode_ena_i = 0 ── 按地址区间：
slv_ports_req_i ──┤   addr_decode(addr) → dec_aw        （同 xbar）
                  │   译码失败 → 选 NoMstPorts（err_slv）
                  │
                  └── interleaved_mode_ena_i = 1 ── 按地址交错：
                      select = addr 的 bank 位 dec_inter_aw
                      并把 bank 位从 addr 中抹掉 → slv_reqs_mod
                              │
                              ▼
            axi_demux (NoMstPorts+1 路，多一路接 err_slv)
                              │
                              ▼  cross 矩阵（纯 assign）
            axi_mux × NoMstPorts  →  mst_ports_req_o
```

bank 位的位置由一个硬编码常量决定：`BankSelLow = 'd16`，即地址**第 16 位**起取 `MstPortsIdxWidth = ⌈log2(NoMstPorts)⌉` 位作为 bank 选择（[src/axi_interleaved_xbar.sv:96-101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L96-L101)）。这意味着相邻的 \(2^{16}=64\,\text{KiB} \) 区块会轮流落到不同 master 端口，实现存储 bank 交错以提升带宽。

> 📌 **源码阅读小提醒**：该处注释写着 `// interleaved select (4kiB blocks)`，但 `BankSelLow = 'd16` 对应的是 64 KiB 粒度（4 KiB 应为第 12 位）。注释与代码不一致，**以代码 `'d16` 为准**。这类「注释陈旧」也是它被标为实验性的旁证之一。

#### 4.2.3 源码精读

**(1) 多预留一个 master 槽位给译码错误从端。** 内部 demux 的输出数组第二维是 `NoMstPorts:0`（即 `NoMstPorts+1` 路），多出的最高下标专门喂 `axi_err_slv`（[src/axi_interleaved_xbar.sv:71-72](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L71-L72)）。译码失败时 `slv_aw_select` 被改写为 `Cfg.NoMstPorts`，事务落入该错误从端返回 DECERR（[src/axi_interleaved_xbar.sv:151-161](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L151-L161)）。这与 `axi_xbar`（经由 `axi_xbar_unmuxed`）的处理思路一致，只是这里把逻辑摊开写在了本模块里。

**(2) 交错模式下的「选端口 + 抹 bank 位」。** 这是最有教学价值的一段。先用地址切片得到 bank 选择（L100-L101），再在 `proc_modify_addr_interleaved` 里把 bank 位从地址中删掉、高位补零（[src/axi_interleaved_xbar.sv:104-119](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L104-L119)）：

```systemverilog
// only modify if interleaved mode is active
if (interleaved_mode_ena_i == 1'b1) begin
  // 抹掉 AW 地址里的 bank 位：高位下移、顶部补零
  slv_reqs_mod[i].aw.addr = { {(MstPortsIdxWidth){1'b0}},
                              slv_ports_req_i[i].aw.addr[Cfg.AxiAddrWidth-1:BankSelHigh],
                              slv_ports_req_i[i].aw.addr[BankSelLow-1:0] };
  ... // AR 同理
end
```

效果是：**只在 bank 位上不同的地址，会被送到不同 master 端口，但下游各自看到的是一段连续的、已压缩的地址空间**——这正是存储交错的语义。随后 `ax_select` 在两种模式间二选一（L151-L161）：交错开则用 `dec_inter_aw`，否则用 `addr_decode` 的结果 `dec_aw`。

**(3) 不走 `axi_xbar_unmuxed`，自己拼 demux + cross + mux。** 注意 `axi_interleaved_xbar` 处于 Level 4，**低于** `axi_xbar`（Level 5），因此它不能、也没有复用 `axi_xbar_unmuxed`，而是直接例化 `addr_decode`、`axi_demux`、`axi_err_slv`、`axi_mux`（[src/axi_interleaved_xbar.sv:191-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L191-L220) 与 [L268-L301](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L268-L301)）。它的 cross 矩阵是**纯组合 `assign`**（[src/axi_interleaved_xbar.sv:242-266](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L242-L266)），不像 `axi_xbar_unmuxed` 那样用 `axi_multicut` 提供 `PipelineStages` 流水线档位。被 `Connectivity` 矩阵剪掉的 (slave,master) 配对还会各自挂一个 `MaxTrans=1` 的 `axi_err_slv` 兜底（L248-L264）。

**(4) 仍保留 `axi_xbar` 的运行期约束。** 与 xbar 一样，它用 `assert property` 强制：任一 AW/AR 处于 `valid && !ready` 期间，`en_default_mst_port_i` 与 `default_mst_port_i` 必须保持稳定，否则 `$fatal`（[src/axi_interleaved_xbar.sv:164-190](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L164-L190)）。注意这些断言在 `VERILATOR`/`XSIM` 下会被跳过（`ifndef` 守卫）。

#### 4.2.4 代码实践

同样是**源码阅读型实践**。

1. **实践目标**：精确刻画「交错开 / 关」两种模式下，同一个地址 `0x0001_8000`（设 `NoMstPorts=2`、`AxiAddrWidth=32`）会被送到哪个 master 端口、下游看到的地址是什么。
2. **操作步骤**：
   - 由 `NoMstPorts=2` 得 `MstPortsIdxWidth = 1`，`BankSelLow=16`，`BankSelHigh=17`。
   - 模式 **关**（`interleaved_mode_ena_i=0`）：行为退化为标准 xbar，端口由 `addr_map_i` 的区间匹配决定。
   - 模式 **开**（`=1`）：`dec_inter_aw = addr[16]`；再用 L110-L112 的拼式算出抹掉 bank 位后的 `addr'`。
3. **需要观察的现象**：地址 `0x0001_8000` 的第 16 位为 1，故交错模式下落到 master 端口 1；下游看到的地址是原地址去掉第 16 位、高位补 0。
4. **预期结果**：把 `0x0001_8000` 按位拆开，验证「端口选择位 = bit[16]」「下游地址 = bit[31:17] 拼上 bit[15:0]，再在最高位补 1 位 0」。
5. **待本地验证**：本库未提供该模块的 TB；若要观察实际波形，需自行写一个最小 testbench（可仿照 u3-l3 的 `tb_axi_lite_regs` 骨架，改用 `AXI_BUS` 与 `axi_interleaved_xbar_intf`）。

#### 4.2.5 小练习与答案

**练习 1**：「实验性」这句话在仓库里出现在哪两处？
**答案**：一是源码注释 [src/axi_interleaved_xbar.sv:18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L18)，二是 README 模块表 [README.md:43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L43)；且 `test/` 下无对应 testbench。

**练习 2**：交错模式下，为什么要从地址里「抹掉」bank 位再送给下游？
**答案**：因为 bank 位已经被用来选 master 端口；若保留，下游每个端口都会看到一片「带空洞」的地址空间。抹掉后，每个 master 端口各自看到一段连续、紧凑的地址，方便后端存储直接寻址。

**练习 3**：`axi_interleaved_xbar` 的 cross 矩阵与 `axi_xbar` 的有何不同？
**答案**：前者是纯组合 `assign`（L242-L266），不提供流水线档位；后者经 `axi_xbar_unmuxed` 用 `axi_multicut` 摆 cross，可由 `Cfg.PipelineStages` 加流水线寄存器。

---

### 4.3 三者选型对比：xbar / xp / interleaved

#### 4.3.1 概念说明

三个模块解决的是「把 N 个 slave 端口连到 M 个 master 端口」这同一个问题，但优化方向不同：

- `axi_xbar`：**通用基准**。地址区间路由、支持默认 master 端口、cross 可加流水线、有完整文档与大量 TB。绝大多数场景的首选。
- `axi_xp`：**为可级联网络而生**。牺牲每 master 端口的 id_remap 面积与并发上限，换取两侧 ID 等宽，便于平铺成 mesh/fat-tree。
- `axi_interleaved_xbar`：**为存储 bank 交错而生**。多一种运行期可切的交错路由，但实验性、无 TB、cross 不可流水。

#### 4.3.2 核心流程（对比维度）

| 维度 | `axi_xbar` | `axi_xp` | `axi_interleaved_xbar` |
|---|---|---|---|
| 编译层级 | Level 5 | **Level 6** | Level 4 |
| master vs slave ID 宽度 | master 更宽（+⌈log2 NoSlv⌉） | **等宽（同构）** | master 更宽（同 xbar） |
| 内部如何拼装 | `axi_xbar_unmuxed` + `axi_mux` 阵列 | **`axi_xbar` + 每 mst 一个 `axi_id_remap`** | 自拼 `addr_decode`+`axi_demux`+`axi_mux`+`axi_err_slv` |
| 路由方式 | 地址区间 | 地址区间（继承 xbar） | 地址区间 **或** 地址交错（运行期切换） |
| 默认 master 端口 | 有（[src/axi_xbar.sv:86-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L86-L90)） | **无**（内部硬接 `'0`，L144-L145） | 有（每 slave 端口一组） |
| cross 矩阵可流水 | 是（`Cfg.PipelineStages`） | 是（继承 xbar） | **否**（纯 `assign`） |
| 文档 / testbench | 有 `doc/axi_xbar.md`、有 `tb_axi_xbar` | 无 doc、**无 TB** | 无 doc、**无 TB**，源码标 experimental |

#### 4.3.3 源码精读（选型背后的代码依据）

- **同构与否**：`axi_xbar` 顶层注释自述 "Fully-connected ... arbitrary number of slave and master ports"（[src/axi_xbar.sv:16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L16)），它把路由与 mux 拆给 `axi_xbar_unmuxed` + `axi_mux`（[src/axi_xbar.sv:97-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L97-L155)）；`axi_xp` 在其外再套一层 id_remap（[src/axi_xp.sv:148-166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xp.sv#L148-L166)）以恢复等宽。选 xp 的唯一硬理由就是「需要等宽端口」。
- **交错与否**：标准 xbar 的路由完全由 `addr_map_i` 决定（区间匹配），没有任何「按位选端口」的分支；`axi_interleaved_xbar` 多出的 `interleaved_mode_ena_i` 输入（[src/axi_interleaved_xbar.sv:51](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L51)）和 L104-L119 的改地址逻辑，是它存在的全部理由。需要 bank 交错才选它。
- **成熟度**：`axi_xbar` 有配套文档 `doc/axi_xbar.md` 与回归用 `test/tb_axi_xbar.sv`（见 u16-l1）；另两者都无。生产环境优先 xbar，xp/interleaved 需自行补验证。

#### 4.3.4 代码实践

1. **实践目标**：为三个给定场景分别选定模块，并用源码行号给出依据。
2. **操作步骤**：对下列每个场景，从上表中挑一个模块，并写一句话理由 + 一个源码引用：
   - (a) 一个 4×4 的片上网络节点，要求所有对外端口 ID 都是 8 位，且要能继续级联。
   - (b) 一个单层 2 主 3 从互连，地址空间按区间划分，希望尽量稳。
   - (c) 一个把连续地址流分散到 2 个 DDR bank 的存储前端互连。
3. **需要观察的现象**：你的选型与理由能否对应到表中的关键差异（同构 / 路由方式 / 成熟度）。
4. **预期结果**：(a) `axi_xp`（同构，L19 注释 + L148 id_remap）；(b) `axi_xbar`（通用基准，L16 注释）；(c) `axi_interleaved_xbar`（交错模式，L51 输入 + L104-L119 改地址），但需提示其实验性（L18）。
5. **待本地验证**：无现成 TB，结论基于源码静态分析。

#### 4.3.5 小练习与答案

**练习 1**：如果你只需要一个普通的 2×2 互连，但误用了 `axi_xp`，会多付出什么代价？
**答案**：每个 master 端口多挂一个 `axi_id_remap`（面积 + 一张 ID 映射表），且并发受 `AxiSlvPortMaxUniqIds` 限制；而这些代价对单层互连毫无收益——本该用 `axi_xbar`。

**练习 2**：能否用 `axi_interleaved_xbar` 替代 `axi_xbar`？
**答案**：把 `interleaved_mode_ena_i` 接 0 时，它的路由行为接近 xbar，但 cross 不可流水、无 TB、被标为实验性。除非确实需要将来切到交错模式，否则不应替代。

**练习 3**：为什么 `axi_xp` 位于全库最高层级 Level 6？
**答案**：因为它例化了 `axi_xbar`（Level 5）和 `axi_id_remap`（Level 2），依赖链最长，按本库「层级 = 最长依赖链 + 1」的规则自然落在最顶层（见 u1-l2）。

## 5. 综合实践

**任务：为一个「双时钟域、含存储交错」的小系统选型并画框图。**

设定：

- 子网 A：1 个 master，ID 宽 8，发出一串连续地址的读请求。
- 子网 B：2 个 DDR bank（2 个 slave 端口），希望连续地址被交错分发到两个 bank 以提升带宽。
- A 与 B 处于不同时钟域。

要求：

1. 在 `axi_xbar`、`axi_xp`、`axi_interleaved_xbar`、`axi_cdc`（u8-l1）中选出需要的模块并说明理由。
2. 画出数据通路框图，标注每个端口的 ID 宽度与路由方式。
3. 指出你方案中唯一的「实验性」风险点，并说明你会如何缓解（例如自写 TB 做随机回归，参考 u3-l2/u3-l3）。

**参考思路**：因为需要 bank 交错，核心互连只能选 `axi_interleaved_xbar`（`interleaved_mode_ena_i=1`），并在其与子网 A 之间插入 `axi_cdc` 做跨时钟域；由于两侧 ID 宽度可保持一致，本例不必引入 `axi_xp`。风险点正是 `axi_interleaved_xbar` 的实验性（[src/axi_interleaved_xbar.sv:18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_interleaved_xbar.sv#L18)）与无 TB，缓解方式是仿照 `tb_axi_xbar` 自建一个 `rand_master → cdc → interleaved_xbar → axi_sim_mem ×2 + scoreboard` 的随机回归（见 u3-l2、u16-l1）。

## 6. 本讲小结

- `axi_xp` = `axi_xbar` + 每 master 端口一个 `axi_id_remap`，目的是让两侧端口 **ID 等宽（同构）**，便于级联成规则网络；位于全库顶层 Level 6。
- 同构的代价是 master 端口多了 id_remap 的面积与 `AxiSlvPortMaxUniqIds` 并发上限；且 `axi_xp` 不对外引出 xbar 的「默认 master 端口」（硬接 `'0`）。
- `axi_interleaved_xbar` 多出 `interleaved_mode_ena_i`：开启后用地址第 16 位起的 bank 位直接选 master 端口，并把这些位从下游地址中抹掉，实现存储 bank 交错。
- `axi_interleaved_xbar` 处于 Level 4，**不复用** `axi_xbar_unmuxed`，而是自拼 `addr_decode + axi_demux + axi_mux + axi_err_slv`，cross 为纯组合、不可流水。
- 「实验性」警告同时见于源码注释（L18）与 README 模块表（L43），且两模块均无 testbench——生产使用前需自补验证。
- 选型口诀：**通用优先 `axi_xbar`；要等宽可级联选 `axi_xp`；要 bank 交错才考虑 `axi_interleaved_xbar`（并自担实验性风险）**。

## 7. 下一步学习建议

- 阅读 `axi_id_remap` 源码（u10-l1），弄清 `axi_xp` 里那张 ID 映射表的分配、查找与回收，理解 `AxiSlvPortMaxUniqIds` 如何成为 xp 的并发瓶颈。
- 结合 u15-l4（异构网络设计实战），把本讲的 `axi_xp`、`axi_interleaved_xbar` 与 `axi_cdc`、`axi_dw_converter` 一起组成跨域、跨宽度、跨 ID 的完整互连，体会「同构端口」在层级化网络里的价值。
- 若计划在生产中使用 `axi_interleaved_xbar`，建议先参考 u16-l1 的定向随机验证方法学，为它补一个对标 `tb_axi_xbar` 的随机回归，把「实验性」这一标签自行消化掉。
