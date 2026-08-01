# cta2warp 与 Warp 派发接口

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `cta2warp` 在 SM（流多处理器）内部扮演的角色——它是 CTA 调度器与 SM 流水线之间的一座「薄桥」。
- 解释一个 workgroup 是如何被拆成若干 warp、每个 warp 又如何被分配到一个本地 `wid`（warp id）上的。
- 画出 `warpReq`（派发一个 warp）与 `warpRsp`（一个 warp 执行完成）这对握手在 `cta2warp` 与 `pipe` 之间的时序关系。
- 理解 warp 完成信号如何逐级回收，最终汇聚成 workgroup 完成（`wg_done`）。
- 区分「warp 完成的回收」与「workgroup 完成的判定」分别发生在哪一级——这是一个容易被误解、但很重要的边界。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面几个概念（它们在 u2-l1、u2-l2 已建立）：

- **workgroup（WG）/ warp（WF）的层级关系**：主机下发的一个 workgroup 含若干个 warp；调度器（CTA scheduler）负责把 workgroup 派发给某个 SM（即 CU）。
- **tag（标签）**：调度器为每个 warp 分配的唯一身份标识，结构上是 `{WG 槽位号, warp 在 WG 内的序号}`（见 [src/define/define.v:199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L199)）。tag 是调度器识别「这是哪个 WG 的哪个 warp」的凭据。
- **cu_handler 的逐 warp 计数**：调度器一侧的 `cu_handler` 会按 tag 记账，当一个 WG 的 `wf_count`（该 WG 含几个 warp）个 warp 全部完成时，才上报 `wg_done`（见 u2-l2）。
- **valid/ready 握手**：`fire = valid && ready`，只有双方都拉高才算一次有效传输。

**一句话直觉**：调度器送来的派发信息可以分成两类——「轻」的控制信号（谁来了、谁走了）和「重」的派发数据（起始 PC、各寄存器基址、wg_id 等）。`cta2warp` 只负责搬运「轻」的那一类：给每个新来的 warp 发一个本地编号 `wid`，记住它的 `tag`，等它执行完再按 `wid` 把 `tag` 还回去。所有「重」数据则**绕过** `cta2warp`，直接从 `sm_wrapper` 的输入端口送进流水线 `pipe`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/gpgpu_top/sm/cta2warp.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v) | 本讲主角。wid 分配器 + tag 记账表，CTA↔pipe 之间的薄桥。 |
| [src/gpgpu_top/sm/sm_wrapper.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v) | SM 顶层。例化 `cta2warp` 与 `pipe`，并把两者的 `warpReq`/`warpRsp` 信号缝合起来。 |
| [src/gpgpu_top/sm/pipeline/pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | SM 流水线主体（下一讲 u3-l1 的主角）。本讲只看它与 `cta2warp` 的握手端口。 |
| [src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v) | warp 调度器。warp「执行完成」信号（`warpRsp`）在这里产生。 |
| [src/common_cell/fixed_pri_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v) | 固定优先级仲裁器，wid 分配的底层元件。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 配置总开关，`NUM_WARP`/`DEPTH_WARP`/`TAG_WIDTH` 等参数定义于此。 |

## 4. 核心概念与源码讲解

### 4.1 cta2warp：wid 分配与 tag 记账

#### 4.1.1 概念说明

`cta2warp` 的模块头注释写得很直白：`cta scheduler 2 warp scheduer`（从 CTA 调度器到 warp 调度器）。它是 SM 内部、紧挨着 `pipe` 流水线前的一个小模块。

要理解它解决什么问题，先看一个矛盾：

- 调度器一侧用 **tag** 来标识每个 warp（tag 里编码了「WG 槽位 + warp 序号」）。
- 但 SM 流水线内部，warp 是用 **wid**（0 到 `NUM_WARP-1` 的本地编号）来寻址的——取指、记分板、寄存器堆、cache 请求全都按 wid 工作，因为 wid 是定长、紧凑、可直接做数组下标的。

于是需要一个「翻译官」：每来一个新 warp，就给它分配一个当前空闲的 wid；同时把它的 tag 存起来；等它执行完，再按 wid 把对应的 tag 查出来还回去。`cta2warp` 就是这个翻译官。

> **一个关键澄清（也是本讲最容易踩坑的地方）**：`cta2warp` **并不**自己把 workgroup 拆成 warp，也**不读** `wf_count`。workgroup→warp 的拆分在更上游的调度器（`gpu_interface`/`cu_handler`）里完成：调度器对同一个 WG 连续发起 `wf_count` 次 `cta_req` 握手，**每次握手只派发一个 warp**。`cta2warp` 收一次、分一个 wid，如此循环。`wf_count` 这个字段本身则**绕过** `cta2warp`，直接被送进 `pipe`（后面 4.2 会用源码证明）。

#### 4.1.2 核心流程

`cta2warp` 内部只有两个寄存器和一段组合逻辑，可以分成「入向（派发）」和「出向（完成）」两条主线：

```
              ┌──────────────── cta2warp 内部 ────────────────┐
入向(派发):   │ cta_req(tag) ──► [wid分配器] ──► warpReq(wid)  │ ──► pipe
              │                     │                          │
              │              idx_using(置位)                    │
              │              data[wid] ← tag                   │
              │                                                │
出向(完成):   │ warpRsp(wid) ◄── pipe                          │
              │   └─► idx_using(清位)                          │
              │   └─► data[wid]查tag ──► cta_rsp(tag) ──► 调度器│
              └────────────────────────────────────────────────┘
```

**入向（派发一个 warp）**：

1. 调度器拉高 `cta_req_valid_i` 并送来该 warp 的 `tag`。
2. `fixed_pri_arb` 对 `~idx_using`（取反 = 空闲位）做固定优先级编码，得到最低位空闲槽的 one-hot：`idx_next_allocate_one`。
3. `one2bin` 把这个 one-hot 转成二进制编号：`idx_next_allocate`（即本次分配的 wid）。
4. 只要还有空槽（`~(&idx_using)`），`cta_req_ready_o=1`，握手成功 → `cta_req_fire`。
5. 把 `warpReq_valid_o=1`、`warpReq_wid_o=idx_next_allocate` 送往 `pipe`；同时把 `tag` 写入 `data[wid]`；并把 `idx_using` 的对应位置 1。

**出向（一个 warp 完成）**：

1. `pipe` 内的 `warp_scheduler` 执行到一条「warp 结束」控制指令，拉高 `warpRsp_valid_i` 并给出 `warpRsp_wid_i`。
2. `cta2warp` 令 `warpRsp_ready_o = cta_rsp_ready_i`（把下游反压透传上来）；握手成功 → `warpRsp_fire`。
3. 按 `warpRsp_wid_i` 查 `data[wid]` 得到当初存的 `tag`，于是 `cta_rsp_valid_o=1`、`cta_rsp_cu2dispatch_wf_tag_done_o=tag` 送回调度器。
4. 同时把 `idx_using` 对应位清 0，释放这个 wid 槽，让它能接纳新的 warp。
5. 调度器一侧的 `cu_handler` 按 tag 计数，累计到 `wf_count` 个 → 产生 `wg_done`（这部分不在 `cta2warp` 内，见 u2-l2）。

`cta2warp` 用一个 `idx_using`（`NUM_WARP` 位）位图来管理 wid 槽：位为 1 表示该 wid 正被一个在跑的 warp 占用。全部占满时 `&idx_using` 为 1，`cta_req_ready_o` 拉低，反压调度器不再派发新 warp。因此 **`NUM_WARP` 决定的是「SM 内可同时驻留（in-flight）的 warp 数上限」**，也就是 wid 槽的个数；而 **`wf_count` 决定的是「这个 WG 一共要派发几个 warp」**。两者一个是容量（槽位数），一个是数量（要派几次），不要混淆。

#### 4.1.3 源码精读

先看端口，体会「轻桥」的简洁。`cta2warp` 对外只暴露与 wid/tag/valid/ready 相关的信号，没有任何寄存器基址、PC 等「重」数据：

[cta2warp 端口](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L17-L38)：左侧 `cta_req_*`/`cta_rsp_*` 连调度器，右侧 `warpReq_*`/`warpRsp_*` 连 `pipe`，`wg_id_lookup_i`/`wg_id_tag_o` 是给 `pipe` 的旁路查询口（4.3 详述）。

两个核心寄存器：

- `idx_using`（[L41](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L41)）：`NUM_WARP` 位的 wid 占用位图。
- `data`（[L45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L45)）：`NUM_WARP*TAG_WIDTH` 位的打包数组，每个 wid 存一个 tag。

**wid 分配器**（组合逻辑核心）：

[cta2warp.v:84-97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L84-L97)：`fixed_pri_arb` 把 `~idx_using`（空闲位）送进去，输出最低空闲位的 one-hot；`one2bin` 再转成二进制 wid。这就是 `idx_next_allocate`。

`fixed_pri_arb` 的实现很经典，用「前缀或」`pre_req` 屏蔽掉非最低位，只保留最低的一个 1（见 [fixed_pri_arb.v:24-27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v#L24-L27)）。所以 wid 分配总是从小编号开始填补——先分 wid 0，再 wid 1，依此类推；wid 0 一旦释放又会被优先复用。

**派发握手与 wid 输出**：

[cta2warp.v:47-61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L47-L61)：
- `cta_req_fire = cta_req_valid_i && cta_req_ready_o`（[L47](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L47)）。
- `cta_req_ready_o = ~(&idx_using)`（[L50](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L50)）：还有空位才接收。
- `warpReq_valid_o = cta_req_fire`、`warpReq_wid_o = idx_next_allocate`（[L57-L58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L57-L58)）：一次派发握手就把 wid 送进流水线。

**完成回收与 tag 查询**：

[cta2warp.v:59-61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L59-L61)：
- `warpRsp_ready_o = cta_rsp_ready_i`、`cta_rsp_valid_o = warpRsp_valid_i`：`cta2warp` 把 `warpRsp` 和 `cta_rsp` **直接对接**，自己不做缓冲——pipe 报告一个 warp 完成，就立刻把它翻译成一个带 tag 的 `cta_rsp` 上报调度器。
- `cta_rsp_cu2dispatch_wf_tag_done_o = data[ TAG_WIDTH*(warpRsp_wid_i+1)-1 -: TAG_WIDTH ]`（[L61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L61)）：按完成的 wid 从 `data` 表里查出当初存的 tag。`-:` 是 Verilog 的「指定起始位、向下取 N 位」切片写法，这里等价于读 `data` 中第 `warpRsp_wid_i` 个 tag 槽。

**两个寄存器的时序更新**：

[cta2warp.v:54-55, 63-82](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L54-L82)：
- `idx_using` 每拍更新为 `(旧值 | 本次分配位) & ~ (本次释放位)`（[L54-L55](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L54-L55) 给出 alloc/dealloc 掩码，[L63-L70](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L63-L70) 是寄存器）。
- `data` 在 `cta_req_fire` 时把新 tag 写入 `data[idx_next_allocate]`（[L72-L82](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L72-L82)）。

> 相关参数（定义在 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)）：默认 `NUM_WARP = 4'b1000`（即 8，[L9](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L9)），故 `DEPTH_WARP = $clog2(NUM_WARP) = 3`，wid 范围 0..7（[L43](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L43)）；`TAG_WIDTH = WG_SLOT_ID_WIDTH + WF_COUNT_WIDTH_PER_WG`（[L199](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L199)），高位是 WG 槽位号、低位是 warp 序号。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里走一遍 wid 分配与 tag 记账，验证「每次握手只派发一个 warp」。

**操作步骤（源码阅读型实践）**：

1. 打开 [cta2warp.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v)。
2. 定位 [L84-L97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L84-L97) 的 wid 分配器，确认 `idx_next_allocate` 完全由 `~idx_using`（空闲位）组合决定，与 `wf_count` 无关。
3. 在 [L50](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L50) 处确认反压条件 `~(&idx_using)`：当 8 个 wid 全被占用时，`cta_req_ready_o=0`，调度器被迫暂停派发。
4. （可选，仿真观察）在 [L47](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L47) 的 `cta_req_fire` 命中处和 [L48](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L48) 的 `warpRsp_fire` 命中处各加一行 `$display`，打印 wid 与 tag，例如：
   ```verilog
   // 示例代码（学习者自行临时添加，验证后可还原）
   always @(posedge clk) begin
     if (cta_req_fire)  $display("[cta2warp] alloc  wid=%0d  tag=%h", idx_next_allocate, cta_req_dispatch2cu_wf_tag_dispatch_i);
     if (warpRsp_fire)  $display("[cta2warp] dealloc wid=%0d  tag=%h", warpRsp_wid_i, data[(`TAG_WIDTH*(warpRsp_wid_i+1)-1)-:`TAG_WIDTH]);
   end
   ```

**需要观察的现象**：每来一个 `cta_req_fire`，wid 从 0 开始单调递增（0,1,2,…），每个 wid 绑定一个不同的 tag；对应的 `warpRsp_fire` 出现时，打印的 tag 与当初分配时一致——证明 `data` 表正确地把 wid 翻译回了 tag。

**预期结果 / 待本地验证**：在 `tc_vecadd`（4w4t 或 4w8t）用例下，应能看到一组「alloc wid=N」之后跟着一组「dealloc wid=N」、tag 一一对应的打印序列。具体打印条数与周期数「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `NUM_WARP` 配成 4，一个 WG 含 6 个 warp（`wf_count=6`），会发生什么？

> **参考答案**：SM 内只有 4 个 wid 槽。前 4 个 warp 派发后 `idx_using=4'b1111`，`&idx_using=1`，`cta_req_ready_o=0`，调度器被反压，必须等某个在跑的 warp 完成、释放一个 wid，才能派发第 5 个；如此直到 6 个全部派完。可见 wid 槽数限制了「同时驻留」的并发度，但不影响最终全部派发完成。

**练习 2**：wid 分配为什么用「固定优先级」而不是轮询？会有什么副作用？

> **参考答案**：固定优先级让低编号 wid 总是被优先复用（wid 0 一释放立刻被占），实现简单、面积小（一个前缀或即可）。副作用是低编号 wid 的寄存器堆/cache 资源更「忙」，可能比高编号 wid 更早出现局部竞争；但本项目通过 `NUM_WARP=8` 等较小规模规避了严重失衡。

---

### 4.2 sm_wrapper 中的接线：薄桥与重数据的分流

#### 4.2.1 概念说明

`sm_wrapper` 是 SM 的顶层。它的工作和 `GPGPU_top` 类似——**只做例化与连线，不做运算**。本模块里它把三件事缝在一起：

1. 把调度器送来的「重」派发数据（`wf_count`、`wf_size`、各基址、`start_pc`、`wg_id` 等）**直接**连到 `pipe` 的 `warpReq_*` 输入；
2. 把 `cta2warp` 产出的 `warpReq_valid`/`warpReq_wid` 也连到 `pipe`；
3. 把 `pipe` 产出的 `warpRsp_valid`/`warpRsp_wid` 连回 `cta2warp`。

换句话说，`cta2warp` 与 `pipe` 共同组成一个「拼好的 warpReq」：`cta2warp` 贡献 `valid` 和 `wid`，`sm_wrapper` 把上游来的其余字段补齐，一起喂给 `pipe`。

#### 4.2.2 核心流程

```
调度器 cta_req_* (全套派发字段，含 tag/wf_count/bases/start_pc/wg_id ...)
        │
        ├──[仅 tag/valid/ready]──► cta2warp ──► warpReq_valid, warpReq_wid ──┐
        │                                                                      │
        └──[其余重数据 wf_count/bases/start_pc/wg_id ...]────────────────────┼─► pipe (合成完整 warpReq)
                                                                              │
        pipe ──► warpRsp_valid, warpRsp_wid ──► cta2warp ──► cta_rsp(tag) ──► 调度器
        pipe ──► wg_id_lookup ──► cta2warp ──► wg_id_tag ──► pipe (旁路查询)
```

#### 4.2.3 源码精读

先看 `sm_wrapper` 里为这套握手准备的内部连线：

[sm_wrapper.v:104-111](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L104-L111)：声明了 `cta2warp_warpReq_valid`、`cta2warp_warpReq_wid`、`cta2warp_warpRsp_ready`、`cta2warp_wg_id_tag`，以及 `pipe` 侧的 `pipe_warpRsp_valid`、`pipe_warpRsp_wid`、`pipe_wg_id_lookup`。

**cta2warp 的例化**（注意它只接了 tag/valid/ready/wid 这几路）：

[sm_wrapper.v:292-313](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L292-L313)：`cta_req_ready_o`/`cta_req_valid_i`/`cta_req_..._wf_tag_dispatch_i` 与顶层 `sm_wrapper` 的对应端口直连；`warpReq_valid_o`→`cta2warp_warpReq_valid`，`warpReq_wid_o`→`cta2warp_warpReq_wid`；`warpRsp_valid_i`←`pipe_warpRsp_valid`，`warpRsp_wid_i`←`pipe_warpRsp_wid`；`wg_id_lookup_i`←`pipe_wg_id_lookup`，`wg_id_tag_o`→`cta2warp_wg_id_tag`。

**pipe 的例化——重数据从这里旁路进入**：

[sm_wrapper.v:370-384](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L370-L384) 是关键证据。注意看：
- `warpReq_valid_i (cta2warp_warpReq_valid)` 和 `warpReq_wid_i (cta2warp_warpReq_wid)`——这两路来自 `cta2warp`；
- 而 `warpReq_dispatch2cu_wg_wf_count_i (cta_req_dispatch2cu_wg_wf_count_i)`、`warpReq_dispatch2cu_start_pc_dispatch_i (...)`、`warpReq_dispatch2cu_wg_id_i (...)` 等**全部直接取自 `sm_wrapper` 的输入端口**，根本没经过 `cta2warp`。

这就用源码证明了 4.1.1 的论断：**`wf_count` 与所有重数据绕过 `cta2warp` 直达 `pipe`**，`cta2warp` 只负责 `valid`+`wid`+`tag`。

反向通路（完成回收）在 [sm_wrapper.v:386-391](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L386-L391)：`warpRsp_ready_i←cta2warp_warpRsp_ready`、`warpRsp_valid_o→pipe_warpRsp_valid`、`warpRsp_wid_o→pipe_warpRsp_wid`、`wg_id_lookup_o→pipe_wg_id_lookup`、`wg_id_tag_i←cta2warp_wg_id_tag`。

#### 4.2.4 代码实践

**实践目标**：在 `sm_wrapper` 里验证「薄桥 vs 重数据」的分流。

**操作步骤**：

1. 在 [sm_wrapper.v:292-313](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L292-L313) 的 `cta2warp` 例化中，数一下连进 `cta2warp` 的 `cta_req_*` 信号有几路（答案：只有 `valid`/`ready`/`wf_tag` 三类）。
2. 在 [sm_wrapper.v:370-384](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L370-L384) 的 `pipe` 例化中，逐行列出 `warpReq_*` 端口，标记哪些来自 `cta2warp_*`、哪些来自顶层 `cta_req_*`。
3. 对比两处，确认 `cta_req_dispatch2cu_wg_wf_count_i` 这一路**没有**出现在 `cta2warp` 例化里。

**需要观察的现象**：`cta2warp` 例化块很短，只连了 tag/valid/ready/wid；而 `pipe` 的 `warpReq_*` 端口列表很长，里面混合了 `cta2warp_*`（valid/wid）和 `cta_req_*`（其余字段）两种来源。

**预期结果**：你会得到一张「`warpReq` 各字段来源表」，其中 `valid`/`wid` 来自 `cta2warp`，`wf_count`/`wf_size`/各 base/`start_pc`/`wg_id` 全部来自顶层直连——完美对应「薄桥 + 重数据旁路」的结构。

#### 4.2.5 小练习与答案

**练习 1**：为什么不让 `cta2warp` 把所有 `cta_req_*` 字段一起寄存转发，反而要让重数据旁路？

> **参考答案**：寄存全套字段意味着 `cta2warp` 内部要复制一整套宽度很大的寄存器（PC、基址等都是 `MEM_ADDR_WIDTH` 位），既费面积又多一级时序。让重数据走纯连线的旁路、`cta2warp` 只管轻量的 valid/wid/tag，模块职责单一、时序更短、复用性更好。

**练习 2**：`warpRsp_ready_o = cta_rsp_ready_i`（[cta2warp.v:59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L59)）把下游反压直接透传，这会带来什么效果？

> **参考答案**：`pipe` 报告 warp 完成时，只有当调度器一侧 `cta_rsp_ready_i` 也为 1（即 `cu_handler` 能接住这个完成信号）时，`warpRsp_fire` 才成立、wid 才被释放。也就是说 warp 完成的回收节奏被上游调度器的处理能力直接约束，避免完成信号在 `cta2warp` 处堆积丢失。

---

### 4.3 warpReq / warpRsp 握手与 warp 完成回收

#### 4.3.1 概念说明

前两节讲了 `cta2warp` 怎么分配 wid、`sm_wrapper` 怎么接线。本节把视角放到**握手协议本身**和**warp 完成信号从哪里来、到哪里去**。

- **warpReq**（派发握手）：`cta2warp`→`pipe`，一次握手 = 让流水线开始跑一个新 warp。关键字段是 `warpReq_valid` + `warpReq_wid`（来自 `cta2warp`），配合 `sm_wrapper` 旁路送来的全套重数据。
- **warpRsp**（完成握手）：`pipe`→`cta2warp`，一次握手 = 一个 warp 跑完了。关键字段是 `warpRsp_valid` + `warpRsp_wid`。
- **wg_id_lookup / wg_id_tag**（旁路查询）：`pipe`→`cta2warp`→`pipe`，一个组合查询回路，让流水线随时能按 wid 查到该 warp 所属的 tag（用于 barrier/栅栏管理）。

一个 warp 「跑完」是如何被检测的？答案在 `warp_scheduler`：当某个 warp 执行到一条带「simt 栈操作」的结束控制指令时，就判定它结束。

#### 4.3.2 核心流程

warp 完成的产生与回收全链路：

```
warp_scheduler:
  执行到结束控制指令(simt_stack_op) 且 warp_control_fire
        │  warp_end=1, warp_end_id=wid
        ▼
  warpRsp_valid_o = warp_end
  warpRsp_wid_o   = warp_end_id
        │
        ▼ (经 sm_wrapper 连线)
cta2warp:
  warpRsp_fire (cta_rsp_ready_i 配合)
        ├─► idx_using 清该 wid 位 (释放槽)
        └─► data[wid] 查 tag ──► cta_rsp_valid + tag ──► 调度器
                                                        │
                                               cu_handler 按 tag 计数
                                                        │
                                          累计到 wf_count ──► wg_done ──► 主机
```

旁路查询回路（独立于完成回收，服务于 barrier）：

```
warp_scheduler:  wg_id_lookup_o = wid  ──► cta2warp: wg_id_tag_o = data[wid] ──► warp_scheduler: end_wg_id = tag 高位
                                                                                       (用于 warp_bar_belong 栅栏位图)
```

#### 4.3.3 源码精读

**warp 完成信号的产生**（在 `warp_scheduler` 内）：

[warp_scheduler.v:116-123](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L116-L123)：
- `warp_end = warp_control_fire && warp_control_simt_stack_op_i`（[L116](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L116)）：当一条控制指令握手且它带 simt 栈操作（即 kernel 末尾的结束/汇合标记）时，该 warp 视为结束。
- `warpRsp_valid_o = warp_end`、`warpRsp_wid_o = warp_end_id`（[L121-L122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L121-L122)）：这就是送给 `cta2warp` 的完成握手。

**旁路查询回路**：

[warp_scheduler.v:173,177](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L173-L177)：
- `wg_id_lookup_o = warp_control_simt_stack_op_i ? warp_end_id : warpRsp_wid_o`（[L173](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L173)）：`warp_scheduler` 把要查的 wid 经 `wg_id_lookup` 送出。
- `cta2warp` 一侧 `wg_id_tag_o = data[ TAG_WIDTH*(wg_id_lookup_i+1)-1 -: TAG_WIDTH ]`（[cta2warp.v:52](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L52)）按 wid 查出 tag 返回。
- 回到 `warp_scheduler`：`end_wg_id = wg_id_tag_i[TAG_WIDTH-1 : WF_COUNT_WIDTH_PER_WG]`（[L177](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L177)），取 tag 的高位（WG 槽位号），用于维护 `warp_bar_belong` 栅栏位图（[L223-L224](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L223-L224)）。

**warp 活跃位图的维护**（与 `cta2warp` 的 `idx_using` 呼应）：

[warp_scheduler.v:255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L255)：`warp_active` 寄存器在 `warpReq_fire` 时置位、在 `warp_end` 时清位——这与 `cta2warp` 的 `idx_using` 几乎是镜像：一个在调度边界记账（`cta2warp`），一个在流水线内部记账（`warp_scheduler`），两者共同保证 wid 生命周期一致。

> **关于「最后一个 warp 完成如何回送 wg 完成」的精确回答**：`cta2warp` **不**感知「最后一个」。它只是把每个 warp 的完成（按 wid）翻译成带 tag 的 `cta_rsp`（按 wid 查 `data`），逐个上报调度器。真正「数到 `wf_count` 判定 WG 完成」的工作在调度器一侧的 `cu_handler`（见 u2-l2）：`cu_handler` 按 tag 归类，每收一个完成就给对应 WG 计数 +1，累计到该 WG 的 `wf_count` 即拉高 `wg_done`。所以 wg 完成的判定**不在 SM 内部**，而在 CTA 调度器内——`cta2warp` 只负责把 wid↔tag 的翻译做对，让调度器能认出每个完成的是谁。

#### 4.3.4 代码实践

**实践目标**：把一条「warp 完成」事件从产生到回送的全链路走通，并定位「wg 完成判定」真正发生的位置。

**操作步骤**：

1. 在 [warp_scheduler.v:116-122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L116-L122) 找到 `warp_end` 的产生条件，确认它由「结束控制指令」触发。
2. 顺着 `warpRsp_valid_o`/`warpRsp_wid_o` 到 [sm_wrapper.v:387-388](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L387-L388)，再到 [cta2warp.v:60-61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L60-L61) 的 `cta_rsp_valid_o`/tag 输出，确认完成信号被翻译成了带 tag 的 `cta_rsp`。
3. 回顾 u2-l2 的 `cu_handler`：确认「`wf_count` 个完成 → `wg_done`」的计数发生在 `cu_handler`，而不是 `cta2warp`。
4. （可选）在仿真中用一个 `wg_id=0`、`wf_count=2` 的小用例，数一下应出现几次 `warpRsp_fire`、几次 `cta_rsp`、最终几个 `wg_done`。

**需要观察的现象**：每个 warp 结束产生恰好一次 `warpRsp_fire`；`cta2warp` 对每次完成输出一次带正确 tag 的 `cta_rsp`；当且仅当一个 WG 的全部 warp 都完成，才在调度器侧出现一次 `wg_done`。

**预期结果 / 待本地验证**：若某 WG 含 `wf_count=N` 个 warp，则应观察到 N 次 wid 回收、N 次 `cta_rsp`，对应 1 次 `wg_done`。具体用例的 N 与周期数「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`cta2warp` 的 `idx_using` 与 `warp_scheduler` 的 `warp_active` 都在追踪 wid 占用，二者有何区别？会不会不一致？

> **参考答案**：`idx_using` 在调度边界（`cta2warp`）记账，决定能否再接纳新 warp；`warp_active` 在流水线内部（`warp_scheduler`）记账，决定取指/发射等是否针对该 wid。二者用同一组事件（`warpReq_fire` 置位、`warp_end` 清位）更新，因此保持一致；它们是同一份「wid 生命周期」在两个抽象层面的镜像。

**练习 2**：如果 `pipe` 报告了一个 `warpRsp_wid`，但 `cta2warp` 的 `data[wid]` 里存的 tag 已被破坏，会出现什么后果？

> **参考答案**：`cta_rsp` 会送回错误的 tag，调度器一侧的 `cu_handler` 会把这次完成错误地记到别的 WG 名下，导致那个 WG 提前/错误地判定完成、而真正的 WG 永远等不到 `wf_count` 个完成而挂死。这正是 `cta2warp` 必须保证「分配时写入、释放前不被覆盖」的原因——它用 `idx_using` 保证一个 wid 在被占用期间不会被重新分配，从而 `data[wid]` 在释放前始终有效。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，绘制一张「一个 workgroup 从进入 SM 到全部 warp 完成」的完整时序—数据流图，并用源码行号标注每个关键节点。

要求在你的图里至少包含并标注以下要素：

1. **入口**：调度器发起 `cta_req`（带 tag），连进 `cta2warp`——标注 [cta2warp.v:21-23](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L21-L23)。
2. **wid 分配**：`fixed_pri_arb`+`one2bin` 产生 `idx_next_allocate`——标注 [cta2warp.v:84-97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L84-L97)。
3. **重数据旁路**：`wf_count`/`start_pc`/各 base/`wg_id` 绕过 `cta2warp` 直达 `pipe`——标注 [sm_wrapper.v:370-384](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L370-L384)。
4. **派发握手**：`warpReq_valid`+`warpReq_wid` 送进 `pipe`，`cta2warp` 写 `data[wid]←tag`、置 `idx_using`——标注 [cta2warp.v:57-58,72-82](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L57-L82)。
5. **完成产生**：`warp_scheduler` 执行到结束指令 → `warpRsp_valid`+`warpRsp_wid`——标注 [warp_scheduler.v:116-122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/warp_scheduler/warp_scheduler.v#L116-L122)。
6. **完成回收**：`cta2warp` 按 wid 查 `data` 得 tag，送 `cta_rsp`，清 `idx_using`——标注 [cta2warp.v:59-61,63-70](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/cta2warp.v#L59-L70)。
7. **wg 完成判定**：在 `cu_handler` 按 tag 计数到 `wf_count` → `wg_done`（不在 SM 内部）——回看 u2-l2。

完成图后，用一段话说明：为什么「wg 完成的判定」必须放在调度器一侧而不是 `cta2warp`？（提示：`cta2warp` 只按 wid 翻译 tag，它不知道一个 WG 总共该有几个 warp；`wf_count` 这个信息从未进入 `cta2warp`。）

## 6. 本讲小结

- `cta2warp` 是 CTA 调度器与 SM 流水线之间的「薄桥」：它只搬运轻量的 valid/wid/tag，所有重数据（`wf_count`、`start_pc`、各基址、`wg_id`）在 `sm_wrapper` 里旁路直连 `pipe`。
- wid 分配由 `fixed_pri_arb`（取最低空闲位）+ `one2bin`（one-hot→二进制）实现，固定优先级、从小编号填补；`NUM_WARP` 决定 wid 槽个数（同时驻留 warp 上限），`wf_count` 决定一个 WG 要派发几个 warp，二者是「容量」与「数量」的区别。
- `cta2warp` 用 `idx_using` 位图管理 wid 占用、用 `data` 表存每个 wid 的 tag；槽满时 `cta_req_ready_o=0` 反压调度器。
- warp「完成」由 `warp_scheduler` 执行到结束控制指令时产生（`warpRsp_valid`+`warpRsp_wid`），经 `cta2warp` 按 wid 查回 tag、翻译成 `cta_rsp` 上报调度器，同时释放 wid 槽。
- **wg 完成的判定不在 `cta2warp` 也不在 SM 内部**：`cta2warp` 只做 wid↔tag 翻译，真正「数到 `wf_count` 产生 `wg_done`」在调度器一侧的 `cu_handler`。
- `cta2warp` 还提供一条 `wg_id_lookup`/`wg_id_tag` 旁路查询回路，供 `warp_scheduler` 按 wid 查 tag、提取 WG 槽位号，用于 barrier/栅铃管理。

## 7. 下一步学习建议

本讲完成了「调度 → SM 入口」的最后一段：现在你已经知道一个 warp 是如何带着 wid 进入流水线的。接下来的学习路径：

- **u3-l1 SM 流水线总览 pipe.v**：进入 `pipe` 内部，建立从取指到写回的完整流水线全景，看 `warpReq` 是如何被 `warp_scheduler` 接收并驱动取指的。
- **u3-l2 取指与指令缓存 icache**：理解拿到 `start_pc` 后，SM 如何按 wid 发起第一条取指请求。
- 如果你想更深入理解 tag 的结构与调度器一侧的计数回收，可以回看 **u2-l2 cu_handler 与 inflight_wg_buffer 派发流程** 中关于 tag 位域与 `wg_done` 产生的部分。
