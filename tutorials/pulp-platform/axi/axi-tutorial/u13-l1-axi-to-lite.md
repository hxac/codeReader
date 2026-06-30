# axi_to_axi_lite 与 axi_lite_to_axi

## 1. 本讲目标

本讲讲解 AXI4 与 AXI4-Lite 之间两个方向的协议转换桥。读完本讲，你应该能够：

- 说清为什么「AXI4 → AXI4-Lite」与「AXI4-Lite → AXI4」两个方向难度天差地别——一个是「丢信息」，一个是「补默认值」。
- 读懂 `axi_lite_to_axi` 如何用一段纯组合逻辑把 Lite 请求包装成合法的 AXI4 单拍事务（补 `id/size/burst/len/last/cache` 等字段）。
- 读懂 `axi_to_axi_lite` 的三级流水线结构：先 `axi_atop_filter` 过滤原子操作、再 `axi_burst_splitter` 拆突发为单拍、最后由 `axi_to_axi_lite_id_reflect` 完成 Lite 信号裁剪与 ID 回射。
- 理解「ID 回射」这个核心机制：Lite 没有 ID，但上游 AXI4 主端要求 B/R 响应带回原始 ID，于是模块用两个 FIFO 暂存请求 ID、在响应返回时再拼回去。
- 看懂两个桥各自的吞吐特性与限制，并能用现成的测试台 `tb_axi_to_axi_lite` / `tb_axi_lite_to_axi` 跑通验证。

## 2. 前置知识

本讲会频繁用到以下概念（均在前序讲义中建立，这里只做最简提示）：

- **AXI4 五通道与 AXI4-Lite 的差别**：完整 AXI4 的 AW/AR 通道有 `id / len / size / burst / lock / cache / prot / qos / region / atop / user` 等一大堆字段，W 通道有 `last`，B/R 通道带 `id`；而 AXI4-Lite 是其**严格子集**——没有 `id`、没有 `burst/len`（恒为单拍）、没有 `atop`、没有 `lock`、没有 `user`，AW/AR 只剩 `addr/prot`，W 只剩 `data/strb`，B 只剩 `resp`，R 只剩 `data/resp`。参见 u2-l3（接口）与 u12-l1（Lite 接口与连接器）。
- **req_t / resp_t 结构体范式**：本库用 `AXI_TYPEDEF_*` 宏把五个通道打包成 `req_t`（请求方驱动信号：AW/W/AR 载荷 + 各 valid + B/R ready）和 `resp_t`（响应方驱动信号：B/R 载荷 + 各 valid + AW/W/AR ready）。参见 u2-l4。
- **突发与拆突发**：`len = 拍数 - 1`，`size` 决定每拍字节数。`axi_burst_splitter` 把多拍突发拆成若干个 `len=0` 的单拍事务，并把多个 B 响应合并为一个。参见 u9-l1。
- **原子操作 ATOPs**：`aw_atop` 字段编码原子操作，ATOP 不被 AXI4-Lite 支持；`axi_atop_filter` 可过滤掉会让不支持 ATOP 的下游出错的原子写。参见 u15-l1（虽为后续讲义，但本讲只需知道「atop_filter 会把原子写改成安全行为」即可）。
- **valid/ready 握手与 in flight / pending**：参见 u1-l3。
- **fifo_v3 与 FallThrough 模式**：`fifo_v3` 是 common_cells 提供的标准 FIFO，`FALL_THROUGH=1` 时写入的数据在同一周期就出现在读端口。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/axi_to_axi_lite.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv) | **AXI4+ATOP → AXI4-Lite** 转换器。顶层例化三个子模块串成流水线，并包含真正做信号翻译的 `axi_to_axi_lite_id_reflect`，外加一个接口外壳 `axi_to_axi_lite_intf`。 |
| [src/axi_lite_to_axi.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv) | **AXI4-Lite → AXI4** 适配器。纯组合逻辑，补全 AXI4 才有的字段，外加接口外壳 `axi_lite_to_axi_intf`。 |
| [test/tb_axi_to_axi_lite.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_axi_lite.sv) | 用随机主端 + 随机 Lite 从端验证「拆突发 + ID 回射」的正确性，并对 Lite 侧握手拍数做断言。 |
| [test/tb_axi_lite_to_axi.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv) | 用定向激励验证一次 Lite 写被正确包装成 AXI4 单拍写。 |

> 说明：`axi_to_axi_lite` 内部还依赖 `axi_atop_filter` 与 `axi_burst_splitter`，但它们的内部实现已在 u9-l1、u15-l1 详细讲解，本讲把它们当作「保证前置条件的黑盒」来对待，只聚焦于桥本身的逻辑。

## 4. 核心概念与源码讲解

### 4.1 两个方向为什么不对称：核心直觉

#### 4.1.1 概念说明

先建立一个最关键的心智模型：**AXI4-Lite 是 AXI4 的严格子集**。这意味着两个转换方向的难度完全不对称：

| 方向 | 做的事 | 难度 | 需要状态吗？ |
|------|--------|------|--------------|
| AXI4-Lite → AXI4 | **补**默认值（id=0、len=0、burst、size…） | 极易，纯组合 | 否 |
| AXI4 → AXI4-Lite | **丢**信息（剥掉 id、拆掉 burst、过滤 atop） | 较难 | 是（要暂存 ID） |

「补默认值」是单向无损的——Lite 本来就没有这些字段，补成什么值都行（只要合法），所以 `axi_lite_to_axi` 只是一段 `assign`。

「丢信息」则是单向有损的，麻烦在于 **ID 不能真的丢**：上游 AXI4 主端发出 `aw.id = 5` 的写，它期望收到的 B 响应也带 `b.id = 5`。可 AXI4-Lite 的 B 响应根本没有 id 字段。所以转换桥必须**自己把请求的 ID 记下来**，等 Lite 那侧把 B 响应返回时，再把记下的 ID 拼回去还给学生。这就是 `axi_to_axi_lite` 需要 FIFO、需要时钟、比反方向复杂得多的根本原因。

#### 4.1.2 核心流程

两个桥的对称结构可以这样画：

```
方向 A：AXI4-Lite → AXI4（axi_lite_to_axi，纯组合）
  Lite req ──(补 id=0,len=0,burst=FIXED,size,last=1,cache=外部)──> AXI4 req
  AXI4 resp ──(剥 id/last/user，只留 resp/data)──> Lite resp

方向 B：AXI4+ATOP → AXI4-Lite（axi_to_axi_lite，三级流水）
  AXI4 req
    │
    ▼ ① axi_atop_filter   : 过滤掉原子操作（Lite 不支持 ATOP）
    │
    ▼ ② axi_burst_splitter: 把多拍突发拆成 len=0 单拍（Lite 只有单拍）
    │
    ▼ ③ id_reflect        : 裁剪信号为 Lite 子集 + 用 FIFO 回射 ID
    │
  AXI4-Lite req
```

关键在于方向 B 里：**①和②都是为了让 ③ 的前置条件成立**。`id_reflect` 假设输入的每个事务都是 `len=0`、`atop=0`、`w.last=1` 的单拍事务——这些假设由前面的过滤器和拆分器保证。如果你单独拿 `id_reflect` 出来用、跳过前两级，仿真断言会直接报错（见 4.4.3）。

#### 4.1.3 小练习与答案

**练习 1**：为什么 `axi_lite_to_axi` 不需要时钟，而 `axi_to_axi_lite` 需要？

**参考答案**：`axi_lite_to_axi` 只补默认值，是无损的纯组合映射，不需要保存任何中间状态；`axi_to_axi_lite` 要丢弃 ID 字段但又必须在响应里把 ID 还回去，必须用 FIFO 暂存请求 ID，而 FIFO 是时序元件，需要时钟和复位。

---

### 4.2 axi_lite_to_axi：补全字段升级到 AXI4

#### 4.2.1 概念说明

`axi_lite_to_axi` 解决的问题是：你有一个只产生 AXI4-Lite 请求的简单主端（比如一个配置寄存器访问模块），却要把它接到一个只接受完整 AXI4 的下游（比如 `axi_xbar`）。Lite 信号是 AXI4 的子集，所以转换的本质就是「给缺失字段填上合法的默认值」。

它有两个对外暴露的版本：
- **结构体版** `axi_lite_to_axi`：端口用 `req_lite_t`/`resp_lite_t` 和 `axi_req_t`/`axi_resp_t`，可综合，是内核版本。
- **接口版** `axi_lite_to_axi_intf`：端口用 `AXI_LITE.Slave` 和 `AXI_BUS.Master`，便于在顶层直接连接口。

两者行为等价，只是表达方式不同（一个用 struct 字段，一个用扁平信号）。

#### 4.2.2 核心流程

转换规则非常机械，逐字段决定 AXI4 端每个字段取什么值：

| AXI4 字段 | 取值 | 说明 |
|-----------|------|------|
| `aw.id` / `ar.id` | `'0` | Lite 无 ID，恒填 0 |
| `aw.addr` / `ar.addr` | 直通 Lite addr | 地址原样保留 |
| `aw.len` / `ar.len` | `'0` | 单拍事务 |
| `aw.size` / `ar.size` | `$clog2(DataWidth/8)` | 一拍覆盖整个数据宽度 |
| `aw.burst` / `ar.burst` | `BURST_FIXED` | 单拍下 burst 类型无关紧要，FIXED 最中性 |
| `aw.cache` / `ar.cache` | 外部输入 `slv_aw_cache_i` / `slv_ar_cache_i` | Lite 无 cache，需设计者从外部喂 |
| `aw.prot` / `ar.prot` | 直通 Lite prot（struct 版）/ `'0`（intf 版） | |
| `lock/qos/region/atop/user` | `'0` | 全部填 0 |
| `w.last` | `'1` | 单拍，永远是最后一拍 |
| `w.data` / `w.strb` | 直通 Lite | |
| 各 `valid` / `ready` | 直通 | 握手信号原样透传 |

响应方向更简单：AXI4 的 B/R 通道多出来的 `id/last/user` 直接丢掉，只把 `resp`（B）或 `data/resp`（R）回给 Lite 侧。

> **关于 cache 的设计取舍**：Lite 通道里没有 cache 字段，但 AXI4 下游可能依赖 cache 位做 bufferable/modifiable 判断（参见 u2-l1 的 `get_awcache`）。因此模块不擅自决定 cache 值，而是要求设计者通过 `slv_aw_cache_i`/`slv_ar_cache_i` 显式提供——这是一个典型的「把策略留给使用者」的设计。

#### 4.2.3 源码精读

模块声明只有四个泛型类型参数和一个 `AxiDataWidth`，端口分 Lite 从端与 AXI4 主端两侧：

[axi_lite_to_axi.sv:18-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L18-L35) —— 模块端口：Lite 请求/响应输入输出 + AXI4 请求/响应输出输入，以及两个外部 cache 输入。

请求方向的整段映射就是下面这一个 `assign`。注意它如何用结构体字面量 `'{...}` 一次性构造完整的 `axi_req_t`，缺失字段用 `default: '0` 兜底：

[axi_lite_to_axi.sv:39-68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L39-L68) —— 请求方向：把 Lite 的 addr/prot 直通，补 `size = AxiSize`、`burst = BURST_FIXED`、`cache = 外部输入`，其余字段 `'0`；`w.last = 1'b1` 标记单拍。

其中 `AxiSize` 的计算体现了 size 字段的含义（每拍 \(2^{\text{size}}\) 字节）：

[axi_lite_to_axi.sv:36](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L36) —— `AxiSize = $clog2(AxiDataWidth/8)`。例如 32 位数据宽度 → 4 字节 → size = 2。

响应方向同样是一个 `assign`，把 AXI4 响应里 Lite 关心的字段挑出来：

[axi_lite_to_axi.sv:70-86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L70-L86) —— 响应方向：B 只取 `resp`、R 只取 `data/resp`，丢弃 AXI4 的 id/last/user。

接口版 `axi_lite_to_axi_intf` 做的是完全相同的事，只是把结构体字段展开成扁平信号赋值（如 `assign out.aw_id = '0; assign out.aw_len = '0; ...`），并额外用 `initial assert` 检查两侧地址/数据宽度一致：

[axi_lite_to_axi.sv:97-160](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L97-L160) —— 接口外壳：逐根信号赋值，等价于结构体版。

#### 4.2.4 代码实践

**实践目标**：通过阅读与运行 `tb_axi_lite_to_axi`，确认一次 Lite 写被正确包装成 AXI4 单拍写。

**操作步骤**：

1. 打开 [test/tb_axi_lite_to_axi.sv:59-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L59-L66)，看 DUT 如何例化：`axi_lite_to_axi_intf` 的 `in` 端接 `axi_lite`（Lite 侧），`out` 端接 `axi`（AXI4 侧），cache 输入接 `'0`。
2. 阅读 [test/tb_axi_lite_to_axi.sv:85-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L85-L95)：Lite 主端用 `axi_lite_driver` 定向发起一次写——`send_aw(addr, prot)` → `send_w(data, strb)` → `recv_b(resp)`。
3. 阅读 [test/tb_axi_lite_to_axi.sv:97-108](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L97-L108)：AXI4 侧用 `axi_driver` 充当从端，`recv_aw` / `recv_w` / `send_b` 完成握手。
4. 在仓库根目录执行仿真（需要 QuestaSim/ModelSim 与 Bender）：
   ```bash
   make compile.log
   make sim-axi_lite_to_axi.log
   ```

**需要观察的现象**：仿真日志中应出现（具体数值为「待本地验证」，因为地址/数据是定向写死的）：
- `AXI-Lite B: resp xx` —— Lite 主端收到的 B 响应。
- `AXI AW: addr deadbeef` —— AXI4 从端收到的写地址（与 Lite 主端发出的地址一致）。
- `AXI W: data deadbeef, strb f` —— AXI4 从端收到的写数据与 strb。

**预期结果**：Lite 侧发出的 `addr/data/strb` 原样出现在 AXI4 侧；AXI4 侧额外看到的 `aw_id=0`、`aw_len=0`、`w_last=1` 都是由桥补上的默认值。日志无 `Error:` / `Fatal:`。

#### 4.2.5 小练习与答案

**练习 1**：如果把一个 Lite 写接到 `axi_lite_to_axi`，下游 AXI4 从端看到的 `aw_burst` 是什么？为什么选这个值？

**参考答案**：看到的是 `BURST_FIXED`（2'b00）。因为 Lite 恒为单拍事务（`len=0`），单拍下地址不递增，burst 类型对行为没有实际影响；选 `FIXED` 表示「地址固定不变」，对单拍而言最中性、最安全。

**练习 2**：为什么 `slv_aw_cache_i` 要做成模块的输入端口，而不是写死成某个常量？

**参考答案**：因为 AXI4-Lite 通道里没有 cache 字段，模块无法从输入推断 cache 语义；而下游 AXI4 可能依赖 cache 位判断 bufferable/modifiable。把 cache 做成显式输入，让系统设计者根据该次访问的属性自行决定，是「策略与机制分离」的体现。

---

### 4.3 axi_to_axi_lite：三级流水线总览

#### 4.3.1 概念说明

`axi_to_axi_lite` 处理的是「把一个完整的 AXI4+ATOP 主端接到一个 AXI4-Lite 从端」的场景。由于 Lite 是子集，转换器必须把 AXI4 多出来的三类信息都处理掉：

1. **原子操作（atop）**：Lite 不支持，必须先过滤。
2. **多拍突发（len > 0）**：Lite 只有单拍，必须拆开。
3. **ID 与其它 AXI4 专属字段**：Lite 没有，必须裁剪；但 ID 要暂存以便回射。

本库没有把这三件事揉进一个大状态机，而是遵循「组合优于配置」的哲学（参见 u1-l1），把它们拆成三个背靠背串联的子模块。顶层 `axi_to_axi_lite` 几乎只做实例化连线。

#### 4.3.2 核心流程

数据从 slave 端口流入，依次经过：

```
slv_req_i ──> [axi_atop_filter] ──> filtered_req
                                      │
                                      ▼
                               [axi_burst_splitter] ──> splitted_req
                                                          │
                                                          ▼
                                                   [axi_to_axi_lite_id_reflect]
                                                          │
                                                          ▼
                                                    mst_req_o (Lite)

响应沿反方向逐级返回：mst_resp_i (Lite) ──> splitted_resp ──> filtered_resp ──> slv_resp_o
```

每一级都对请求做一次「净化」，让下一级的前置条件更宽松：
- 经过 atop_filter 后，输出不再有原子操作。
- 经过 burst_splitter 后，输出全部是 `len=0` 单拍。
- 送到 id_reflect 时，已经满足它要求的 `atop==0 && len==0 && w.last==1`。

#### 4.3.3 源码精读

顶层模块声明。注意几个关键参数：`AxiMaxWriteTxns` / `AxiMaxReadTxns` 决定内部 ID FIFO 深度（即最大在途事务数）；`FullBW` 透传给 burst_splitter 的内部 ID 队列；`FallThrough` 控制 ID FIFO 是否走 fall-through 模式：

[axi_to_axi_lite.sv:18-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L18-L41) —— 顶层端口：时钟/复位/test、AXI4 slave 端、AXI4-Lite master 端。

第一级，原子操作过滤（其内部实现见 u15-l1，此处当黑盒）：

[axi_to_axi_lite.sv:47-59](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L47-L59) —— `i_axi_atop_filter`：把上游可能产生的原子写改成下游能安全处理的形式，保证 `filtered_req` 里不含 ATOP。

第二级，突发拆分（其内部实现见 u9-l1）：

[axi_to_axi_lite.sv:62-79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L62-L79) —— `i_axi_burst_splitter`：把任意突发拆成一串 `len=0` 单拍事务，多个 B 响应合并为一个；保证 `splitted_req` 全是单拍。

第三级，真正的信号翻译与 ID 回射（下一节 4.4 详讲）：

[axi_to_axi_lite.sv:82-99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L82-L99) —— `i_axi_to_axi_lite_id_reflect`：把 AXI4 信号裁剪成 Lite 子集，并用 FIFO 回射 ID。

顶层还做了基本的参数断言（`AxiIdWidth/AxiAddrWidth/AxiDataWidth > 0`）：

[axi_to_axi_lite.sv:101-110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L101-L110) —— 参数合法性检查。

接口外壳 `axi_to_axi_lite_intf` 展示了标准的「接口外壳 + 结构体内核」范式：先用 `AXI_TYPEDEF_*` / `AXI_LITE_TYPEDEF_*` 宏声明类型，再用 `AXI_ASSIGN_TO_REQ` / `AXI_LITE_ASSIGN_FROM_REQ` 等宏在 `AXI_BUS`/`AXI_LITE` 接口与 struct 之间搬数据，最后例化结构体内核：

[axi_to_axi_lite.sv:250-326](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L250-L326) —— 接口外壳：类型声明 → 接口/struct 互连 → 例化内核。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码与注释，确认三级流水线中每一级的职责边界，以及为什么必须按「atop_filter → burst_splitter → id_reflect」这个顺序。

**操作步骤**：

1. 在 [src/axi_to_axi_lite.sv:42-44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L42-L44) 找到三对中间信号：`filtered_req/filtered_resp`、`splitted_req/splitted_resp`。
2. 对照三处实例化，画出每级「输入信号 → 输出信号」的对应关系。
3. 思考：如果把 burst_splitter 放在 atop_filter **之前**会怎样？

**需要观察的现象 / 预期结论**：`atop_filter` 必须在 `burst_splitter` 之前——因为 `axi_burst_splitter` 本身不支持 ATOP（遇到 ATOP 会回 SLVERR，见其文档注释 [src/axi_burst_splitter.sv:24-26](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_splitter.sv#L24-L26)）。因此顺序不能调换。这是「组合」时必须尊重依赖关系的典型例子。

> 待本地验证：可尝试在一份本地分支里把两级顺序对调，跑 `tb_axi_to_axi_lite`（先把主端的 `AXI_ATOPS` 改成 `1'b1`），观察是否出现 SLVERR 或断言失败。

#### 4.3.5 小练习与答案

**练习 1**：`AxiMaxWriteTxns` 这个参数同时出现在 `axi_atop_filter`、`axi_burst_splitter` 和 `axi_to_axi_lite_id_reflect` 三处实例化里（[L48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L48)、[L65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L65)、[L84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L84)），为什么三级要用同一个值？

**参考答案**：整个流水线是一条串联的「在途事务通道」，任一级的容量（最大在途数）都不能小于其上下游，否则会成为瓶颈或导致反压不一致。三处共用同一个 `AxiMaxWriteTxns`，保证三级都能容纳同样的最大在途写事务数，形成一致的流控边界。

---

### 4.4 axi_to_axi_lite_id_reflect：ID 回射与信号裁剪

#### 4.4.1 概念说明

`axi_to_axi_lite_id_reflect`（定义在与顶层同一个文件里）是整个桥的真正核心，做两件事：

1. **信号裁剪**：把 AXI4 的 AW/AR/W 请求裁剪成 Lite 子集（AW/AR 只留 `addr/prot`，W 只留 `data/strb`），丢弃 `id/len/burst/atop/cache/user` 等。
2. **ID 回射**：用两个 FIFO（一个写方向、一个读方向）暂存请求 ID，在 B/R 响应返回时把 ID 拼回去。

它对输入有强假设——`atop==0`、`len==0`、`w.last==1`（这些由上游两级保证），并用 `assume property` 断言强制检查。如果有人单独例化它而跳过 atop_filter / burst_splitter，断言会触发 `$fatal`。

#### 4.4.2 核心流程

ID 回射的时序逻辑（以写方向为例）：

```
时刻 T1: AXI4 AW 握手 (aw_valid & aw_ready)
         --> 把 slv_req.aw.id 压入 aw_id_fifo (push)
         --> 同时把 addr/prot 裁剪后送给 Lite 的 AW

时刻 T2: Lite 侧完成写，回 B 响应 (b_valid)
         --> 检查 aw_id_fifo 非空 (~aw_empty)，从 FIFO 读出当初存的 id
         --> 把 {id=reflected_id, resp=Lite_b.resp} 拼成 AXI4 B 响应回给 slave
         --> B 握手时弹出 FIFO (pop)
```

读方向完全对称：AR 握手时压 `ar.id`，R 响应返回时弹出并拼回。

流控（反压）通过 FIFO 的 full/empty 信号实现：

- 请求方向：`aw_ready = Lite.aw_ready & ~aw_full`、`aw_valid(到Lite) = slv.aw_valid & ~aw_full`。FIFO 满时不再接收新 AW，避免 ID 丢失。
- 响应方向：`b_valid(到slave) = Lite.b_valid & ~aw_empty`、`b_ready(到Lite) = slv.b_ready & ~aw_empty`。FIFO 空时（没有 ID 可拼）不向上游回 B，避免回射出无效 ID。

FIFO 深度为 `AxiMaxWriteTxns` / `AxiMaxReadTxns`，因此最大在途事务数被这两个参数限定。

#### 4.4.3 源码精读

先看请求方向的裁剪。`mst_req_o`（送给 Lite）只保留 `addr/prot` 和 `data/strb`，valid 信号被 `~aw_full`/`~ar_full` 门控：

[axi_to_axi_lite.sv:207-226](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L207-L226) —— 请求方向：AW/AR 只取 `addr/prot`、W 只取 `data/strb`，其余字段被 `default: '0` 丢弃；`aw_valid`/`ar_valid` 被 FIFO 非满门控，`b_ready`/`r_ready` 被 FIFO 非空门控。

再看响应方向的 ID 回射。`slv_resp_o`（回给 AXI4 slave）里的 B/R 通道把 Lite 响应的 `resp`/`data` 与 FIFO 读出的 `reflected_id` 拼在一起：

[axi_to_axi_lite.sv:144-163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L144-L163) —— 响应方向：`b.id = aw_reflect_id`、`r.id = ar_reflect_id`，`r.last` 恒为 1（单拍）；各 valid 被 FIFO 非空门控，各 ready 被 FIFO 非满/非空门控。

写方向 ID FIFO（标准 `fifo_v3`）。注意 push/pop 条件与深度：

[axi_to_axi_lite.sv:166-184](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L166-L184) —— `i_aw_id_fifo`：`FALL_THROUGH` 来自参数、`DEPTH = AxiMaxWriteTxns`、存 `id_t`；AW 握手时 push `slv_req_i.aw.id`，B 握手时 pop。

读方向 ID FIFO 完全对称：

[axi_to_axi_lite.sv:187-205](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L187-L205) —— `i_ar_id_fifo`：AR 握手 push、R 握手 pop，深度 `AxiMaxReadTxns`。

最后是输入前置条件的断言——它们把「必须先经过 atop_filter + burst_splitter」这一隐性依赖显式化：

[axi_to_axi_lite.sv:231-242](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L231-L242) —— 四条 `assume property`：`aw.atop==0`、`aw.len==0`、`w.last==1`、`ar.len==0`。违反即 `$fatal`。

> **关键洞察**：`assign aw_push = mst_req_o.aw_valid & slv_resp_o.aw_ready`（[L166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_axi_lite.sv#L166)）用的是**门控后**的 `aw_valid`/`aw_ready`，而不是原始的 slave 信号。这保证只有当 AW 真正被 Lite 接受、且 ID 已成功存入 FIFO 时才记一笔——计数与实际握手严格对齐，不会漏记或多记。

#### 4.4.4 代码实践

**实践目标**：用现成的 `tb_axi_to_axi_lite` 验证「一个多拍 AXI4 写被拆成多个单拍 Lite 写，且响应正确合并、ID 正确回射」。这正是本讲指定的实践任务。

**操作步骤**：

1. 打开 [test/tb_axi_to_axi_lite.sv:66-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_axi_lite.sv#L66-L80)，看 DUT `axi_to_axi_lite_intf` 如何接线：slave 端接 `AXI_BUS axi`，master 端接 `AXI_LITE axi_lite`，最大在途写/读都设为 10。
2. 阅读 [test/tb_axi_to_axi_lite.sv:82-99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_axi_lite.sv#L82-L99)：上游用 `axi_rand_master`（会产生随机长度的多拍突发，`MAX_READ_TXNS=20`、`MAX_WRITE_TXNS=20`、`AXI_ATOPS=0`），下游用 `axi_lite_rand_slave`。
3. 阅读 [test/tb_axi_to_axi_lite.sv:115-134](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_axi_lite.sv#L115-L134)：主端配置三段内存区（不同 cache 属性），然后 `axi_drv.run(1000, 2000)` 发起 1000 次读 + 2000 次写随机事务。
4. 重点阅读拍数计数与自检 [test/tb_axi_to_axi_lite.sv:149-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_to_axi_lite.sv#L149-L177)：用五个计数器分别统计 Lite 侧 AW/W/B/AR/R 的握手次数，最后断言 `aw_cnt == w_cnt == b_cnt` 与 `ar_cnt == r_cnt`。
5. 在仓库根目录执行：
   ```bash
   make compile.log
   make sim-axi_to_axi_lite.log
   ```

**需要观察的现象**：仿真结束后日志会打印五个计数（具体数值取决于随机种子，「待本地验证」），例如形如：
```
AXI4-Lite AW count: <N>
AXI4-Lite  W count: <N>
AXI4-Lite  B count: <N>
AXI4-Lite AR count: <M>
AXI4-Lite  R count: <M>
```

**预期结果**：
- `aw_cnt == w_cnt == b_cnt` 成立——无论上游发了多少拍的多拍写，到 Lite 侧每一笔都变成「1 个 AW + 1 个 W + 1 个 B」的单拍写，因此三类握手次数相等。这正是「拆成 4 次单拍写且响应正确合并」的量化体现：若上游发了 4 拍写的突发，Lite 侧会看到 4 个独立的单拍写、各自带 1 个 B；多个 B 由 `burst_splitter` 合并后回给上游变成 1 个 B。
- `ar_cnt == r_cnt` 成立——每笔读都是 1 AR + 1 R。
- 日志末尾出现 `All AXI4+ATOP Bursts converted to AXI4-Lite`，且无 `Error:` / `Fatal:`。

> **想进一步定向验证「4 拍写」？** 可仿照 `tb_axi_lite_to_axi` 的写法，把 `tb_axi_to_axi_lite` 里的随机主端换成手搓的 `axi_driver`，定向发一个 `len=3`（即 4 拍）的 INCR 写，然后在 Lite 侧计数器里确认恰好多了 4 次 AW/W/B 握手。该改动属于测试台修改、不影响源码，预期 Lite 侧新增 4 个单拍写。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `b_valid`（回给 slave）要写成 `mst_resp_i.b_valid & ~aw_empty`，而不是直接 `mst_resp_i.b_valid`？

**参考答案**：Lite 侧返回 B 时本身不带 ID。如果 `aw_id_fifo` 是空的，说明此时没有已记录的请求 ID 可以拼回——直接转发会把 `b.id` 设成无效值（FIFO 在空时的读输出未定义）。用 `~aw_empty` 门控 `b_valid`，确保只在「有 ID 可拼」时才向上游声明 B 有效，从而保证回射出的 ID 一定合法。

**练习 2**：`AxiMaxWriteTxns = 1` 时，写方向的并发能力会变成怎样？

**参考答案**：写方向 ID FIFO 深度为 1，意味着同一时刻最多只能有 1 个未完成的写事务（AW 已握手但 B 未返回）。第二个 AW 会被 `~aw_full` 反压挡住，直到第一个 B 返回并 pop 出 FIFO。因此 `AxiMaxWriteTxns` 直接决定了写方向的最大在途并发数；设得太小会显著降低吞吐。

**练习 3**：模块为什么用 `assume property`（而不是 `assert property`）来检查 `aw.atop==0`？

**参考答案**：`assume` 表示「模块假设输入满足该条件」，把满足条件的责任交给了上游（即顶层必须先接 atop_filter）；而 `assert` 表示「模块自己保证」。这里 atop==0 是由上游 atop_filter 保证的前置条件，不是本模块自己保证的，因此用 `assume` 语义更准确。若上游没满足，`assume` 会报「被违反」并 `$fatal`，起到防护作用。

## 5. 综合实践

把两个方向串起来，搭一个**双向回环**：`AXI4 主端 → axi_to_axi_lite → AXI4-Lite → axi_lite_to_axi → AXI4 从端`。这是一个验证两个桥互为逆运算的好实验。

**任务**：

1. 新建一个测试台（可基于 `tb_axi_to_axi_lite` 改），在 `axi_to_axi_lite` 的 Lite master 端口与一个 `axi_lite_to_axi` 的 Lite slave 端口之间，用 `AXI_LITE_ASSIGN` 直连（中间可再加一个 `axi_lite_join` 或 `axi_delayer` 制造时序抖动）。
2. 上游用 `axi_rand_master`，最下游用 `axi_sim_mem` + `axi_scoreboard`（参见 u3-l2）做自检。
3. 跑若干随机种子，确认 scoreboard 无失配。

**预期结论**：从功能上看，`axi_to_axi_lite` 再接 `axi_lite_to_axi` 后，信号被「裁剪又补回」，但补回的 `id` 恒为 0、`len` 恒为 0、`burst` 恒为 FIXED——也就是说，这个回环会把**任意 AXI4 突发压扁成一串 AXI4 单拍事务**（id 信息也可能丢失，取决于上游 master 是否用非零 id）。因此：
- 若上游 master 只发 `id=0` 的单拍事务，回环前后行为等价，scoreboard 应通过。
- 若上游发多拍突发或带非零 id，回环后下游看到的已是「压扁」后的等价单拍序列；你需要让 scoreboard/sim_mem 的期望与之匹配，或限定激励只用单拍 id=0 事务。

这个实验能帮你直观体会「Lite 是子集、转换是有损的」这一核心事实。

## 6. 本讲小结

- **两个方向不对称**：AXI4-Lite → AXI4 只需补默认值（纯组合、无损）；AXI4 → AXI4-Lite 需要丢信息（拆突发、过滤 atop、裁剪字段），且 ID 必须暂存回射，因而需要时钟与状态。
- **`axi_lite_to_axi` 是一段 `assign`**：补 `id=0`、`len=0`、`burst=FIXED`、`size=$clog2(DataWidth/8)`、`w.last=1`、`cache=外部输入`，其余字段清零；响应方向丢弃 AXI4 专属的 id/last/user。
- **`axi_to_axi_lite` 是三级流水线**：`axi_atop_filter`（过滤原子）→ `axi_burst_splitter`（拆突发为单拍）→ `axi_to_axi_lite_id_reflect`（裁剪 + ID 回射），顺序不可调换。
- **ID 回射是核心机制**：用两个 `fifo_v3`（深度 = 最大在途事务数）分别暂存 AW/AR 的 id，在 B/R 响应返回时拼回；用 FIFO 的 full/empty 做请求反压与响应门控，保证回射 ID 合法。
- **前置条件由上游保证**：`id_reflect` 用 `assume property` 强制输入满足 `atop==0 / len==0 / w.last==1`，这把「必须先接 atop_filter + burst_splitter」的隐性依赖显式化。
- **吞吐限制**：写/读方向的最大在途并发分别由 `AxiMaxWriteTxns` / `AxiMaxReadTxns`（即 FIFO 深度）决定；突发被拆成单拍后，Lite 侧的握手拍数会多于上游（一次 N 拍写在 Lite 侧变成 N 个单拍写）。

## 7. 下一步学习建议

- **u13-l2（axi_lite_to_apb）**：继续看 Lite 往外转的桥——从 AXI4-Lite 转 APB4，体会协议转换时「字段映射 + 时序状态机」的另一种典型写法（APB 有 setup/access 两相时序，比纯组合的 `axi_lite_to_axi` 多一层状态）。
- **回顾 u9-l1（axi_burst_splitter）**：本讲把 burst_splitter 当黑盒，建议回头精读它如何把多拍拆单拍、如何合并多个 B 响应，从而完整理解 `axi_to_axi_lite` 的中间一级。
- **回顾 u15-l1（ATOPs 与 axi_atop_filter）**：理解原子操作编码与 atop_filter 的过滤逻辑，看清 `axi_to_axi_lite` 第一级在过滤什么。
- **动手扩展综合实践**：把第 5 节的回环实验真的搭出来，并尝试在 Lite 段插入 `axi_delayer`（u4-l3）制造握手抖动，检验整套转换在随机时序下是否依然正确——这是验证协议桥健壮性的标准手法。
