# AXI 跨时钟域与通道 FIFO

## 1. 本讲目标

本讲解决一个问题：**当 AXI 总线两端的 master 和 slave 跑在两个互不同步的时钟上时，怎么把整条总线安全地搬过时钟域？**

读完本讲你应该能够：

- 说清 AXI 五个通道（AR / R / AW / W / B）各自跨时钟域的方向，以及 hdl-modules 为什么「每个通道单独配一个异步 FIFO」。
- 读懂 `axi_read_cdc` / `axi_write_cdc` 的顶层接线，并指出每个通道 FIFO 的写时钟、读时钟分别接到了哪个时钟域。
- 解释「记录打包成窄向量再进 FIFO」这一步：为什么 `valid` / `ready` 不进 RAM、只打包实际用到的负载位。
- 看懂请求通道（AR / AW / W）与响应通道（R / B）在 FIFO 内部「写读方向相反」的设计，并理解 `depth=0` 直通与 `asynchronous` 切换的机制。
- 画出 `axi_*_cdc` 内部对 `fifo_wrapper` → `asynchronous_fifo` → `resync_counter` 的复用链，并知道约束该从哪里拿。

---

## 2. 前置知识

本讲建立在前几讲的概念之上，先用三段话把要紧的背景补齐：

- **AXI 通道是独立的（承接 u5-l1、u5-l2）。** AXI4 把一次事务拆成五条「通道」，每条通道都是一组独立的 ready/valid 握手信号：读地址 `AR`、读数据 `R`、写地址 `AW`、写数据 `W`、写响应 `B`。master 和 slave 可以在每条通道上用各自的延迟和吞吐推进，互不阻塞。hdl-modules 用 `axi_pkg` 里的 record（如 `axi_read_m2s_t = ar + r`）把这些信号捆成结构体。

- **多比特向量不能逐位同步（承接 u3-l2）。** 跨时钟域（CDC）时，一根单比特信号可以用两级 `async_reg` 同步链兜住亚稳态；但一根「同时变化的多比特向量」各比特布线延迟不同，目的域会采到新旧混杂的脏值（bit coherency 问题）。解决套路之一就是**异步 FIFO**——它内部用格雷码读写指针做单比特化的安全跨域（承接 u3-l1 的 `resync_counter`、u4-l2 的 `asynchronous_fifo`），数据则整字写入 RAM、按指针协议保证读写不冲突。

- **`fifo_wrapper` 是一个三态开关（承接 u4-l2）。** 同一个 `fifo_wrapper` 实体，用 `depth=0` 表示「直通、不要 FIFO」，用 `use_asynchronous_fifo` 在同步 FIFO 与异步 FIFO 之间切换。本讲的通道 FIFO 几乎全部工作都委托给它。

一句话总结动机：**AXI 跨时钟域 = 给每条通道挂一个异步 FIFO；每个 FIFO 把 record 打包成最窄的位向量，再用 `fifo_wrapper` 切到异步模式，由 `asynchronous_fifo` 完成真正的 CDC。**

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `modules/axi/src/axi_read_cdc.vhd` | 读总线（AR+R）CDC 顶层：把两个通道各接到一个异步 FIFO |
| `modules/axi/src/axi_write_cdc.vhd` | 写总线（AW+W+B）CDC 顶层：把三个通道各接到一个异步 FIFO |
| `modules/axi/src/axi_address_fifo.vhd` | 地址通道（AR 或 AW）FIFO：record↔slv 打包 + `fifo_wrapper` |
| `modules/axi/src/axi_b_fifo.vhd` | 写响应通道（B）FIFO：注意它是「反向」跨域 |
| `modules/axi/src/axi_r_fifo.vhd` | 读数据通道（R）FIFO：带 `output_level`、可开 packet 模式 |
| `modules/axi/src/axi_w_fifo.vhd` | 写数据通道（W）FIFO：可开 packet 模式 |
| `modules/axi/src/axi_pkg.vhd` | record 类型定义、`to_slv` / `to_axi_m2s_a` 等打包/解包函数 |
| `modules/axi/test/tb_axi_cdc.vhd` | 现成的 CDC 连通性测试台：两个异步时钟 + master/slave BFM |
| `modules/fifo/scoped_constraints/asynchronous_fifo.tcl` | 异步 FIFO 必须配套的时序约束（本讲会反复提到它）|

> 提示：`axi` 模块**没有自己的 `scoped_constraints/` 目录**。因为真正的 CDC 由 `fifo` 模块里的 `asynchronous_fifo` 完成，所以约束要直接复用 `fifo.asynchronous_fifo` 的那份 `.tcl`。

---

## 4. 核心概念与源码讲解

### 4.1 为什么按通道拆 FIFO：AXI CDC 的总体设计

#### 4.1.1 概念说明

最朴素的想法是「把整条 AXI 总线塞进一个大 CDC 模块」。但 AXI 有个特点：**五条通道相互独立、节奏差异巨大**。

- 地址通道（AR / AW）：一次突发（burst）只发**一个**地址 beat，非常稀疏。
- 数据通道（R / W）：一次突发要搬**很多**个数据 beat，非常密集。
- 响应通道（B）：整段写突发结束后才回**一个** B。

如果用一个 FIFO 装下所有通道，FIFO 的深度和位宽会被最繁忙的数据通道撑大，而稀疏的地址通道却被迫排队等数据通道腾位置——地址延迟会反过来拖慢整条事务。因此 hdl-modules 的做法是**每条通道各配一个独立 FIFO**：

- 每个 FIFO 的深度、RAM 类型可以单独按该通道的流量定制；
- 地址路径和数据路径彻底解耦，互不阻塞；
- 每条通道就是一个标准的 ready/valid 流，CDC 套路完全统一。

这正好承接 u5-l1 / u5-l2 反复出现的项目取向：**组合优于重写、按通道精细控制面积**。

#### 4.1.2 核心流程

关键要分清「方向」。约定如下：

- **input 侧 = master，跑 `clk_input`**；
- **output 侧 = slave，跑 `clk_output`**；
- 两个时钟异步。

于是五条通道天然分成两组方向相反的流：

| 通道 | 内容 | 数据流向 | 跨域 FIFO 写/读时钟 |
| --- | --- | --- | --- |
| `AR` | master 发的读地址 | input → output（master→slave） | 写=`clk_input`，读=`clk_output` |
| `R` | slave 回的读数据 | output → input（slave→master） | 写=`clk_output`，读=`clk_input` |
| `AW` | master 发的写地址 | input → output | 写=`clk_input`，读=`clk_output` |
| `W` | master 发的写数据 | input → output | 写=`clk_input`，读=`clk_output` |
| `B` | slave 回的写响应 | output → input | 写=`clk_output`，读=`clk_input` |

**请求类通道**（AR / AW / W）从 master 流向 slave；**响应类通道**（R / B）从 slave 流回 master。这两组方向相反，但用的是同一套通道 FIFO 实体——区别只在 FIFO 内部把「写」和「读」分别接到哪一侧的时钟上（详见 4.3）。

整个读路径的 CDC 流程可以用伪流程描述：

```
读事务 CDC 流程（axi_read_cdc）:
1. master 在 clk_input 拉高 AR.valid + 地址/控制字段
2. axi_address_fifo 把这一拍 record 打包成 slv，在 clk_input 写入异步 FIFO
3. 异步 FIFO 用格雷码指针把「有新数据」安全地传到 clk_output 侧
4. clk_output 侧 slave 看到 AR.valid，回若干拍 R 数据（R.valid + data/resp/id）
5. axi_r_fifo 把 R 这一拍打包成 slv，在 clk_output 写入另一个异步 FIFO
6. master 在 clk_input 侧从 FIFO 读出 R 数据 → 完成一次读
```

注意：AR 用一个 FIFO、R 用**另一个** FIFO，各自独立；它们之间唯一的「协议联系」是 AXI 本身的 outstanding 事务语义，CDC 模块并不在两条 FIFO 之间拉任何控制信号。

#### 4.1.3 源码精读：顶层接线

先看读总线 CDC。`axi_read_cdc` 的端口就是「一个 input 侧的读总线 + 一个 output 侧的读总线 + 两个时钟」，外加一个 R FIFO 的 level 旁路：

[axi_read_cdc.vhd:29-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_cdc.vhd#L29-L50) —— 实体声明。注意 `output_data_fifo_level` 这个输出：它反映 R FIFO 在 slave（output）侧已经攒了多少拍数据，可以喂给 u5-l2 讲过的 `axi_read_throttle` 做节流。

实现体里只做两件实例化，分别对应 AR 通道和 R 通道：

```vhdl
-- AR 通道：用 axi_address_fifo，开 asynchronous
axi_address_fifo_inst : entity work.axi_address_fifo
  generic map (
    id_width => id_width, addr_width => addr_width,
    asynchronous => true, depth => address_fifo_depth, ...
  )
  port map (
    clk => clk_output,          -- FIFO 的 clk（读侧 = slave 域）
    input_m2s => input_m2s.ar,  -- master 的 AR 请求
    output_m2s => output_m2s.ar,-- 给 slave 的 AR 请求
    clk_input => clk_input      -- 写侧时钟 = master 域
  );
```

完整 AR 接线见 [axi_read_cdc.vhd:56-75](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_cdc.vhd#L56-L75)。接着是 R 通道：

```vhdl
-- R 通道：用 axi_r_fifo，同样开 asynchronous
axi_r_fifo_inst : entity work.axi_r_fifo
  generic map (
    id_width => id_width, data_width => data_width,
    asynchronous => true, depth => data_fifo_depth,
    enable_packet_mode => enable_data_fifo_packet_mode, ...
  )
  port map (
    clk => clk_output,
    input_m2s  => input_m2s.r,   -- master 给 slave 的 R 握手（ready）
    output_s2m => output_s2m.r,  -- slave 回的 R 数据
    output_level => output_data_fifo_level,
    clk_input => clk_input
  );
```

完整 R 接线见 [axi_read_cdc.vhd:78-99](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_cdc.vhd#L78-L99)。

> 细节：两个通道 FIFO 都把顶层端口 `clk` 接到 `clk_output`、把 `clk_input` 接到 `clk_input`。至于「谁在 `clk_input` 上写、谁在 `clk_input` 上读」，是在**每个通道 FIFO 内部**根据方向决定的（AR 在 `clk_input` 上写，R 在 `clk_input` 上读）。这套约定让顶层接线整齐划一。

写总线 CDC 结构完全对称，只是多了一条通道：`AW` 复用同一个 `axi_address_fifo`，`W` 用 `axi_w_fifo`，`B` 用 `axi_b_fifo`，三者都开 `asynchronous => true`。见 [axi_write_cdc.vhd:57-119](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_write_cdc.vhd#L57-L119)。写总线的实体声明多了 `response_fifo_depth` / `response_fifo_ram_type` 两个 generic，因为 B 通道也需要一个有深度的 FIFO（[axi_write_cdc.vhd:39-40](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_write_cdc.vhd#L39-L40)）。

两个 CDC 实体的文件头都强调同一件事：

[axi_read_cdc.vhd:9-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_cdc.vhd#L9-L16) —— 注释明确：「用异步 FIFO 跨 AR 和 R 通道」，并且 **必须套用 `fifo.asynchronous_fifo` 的约束**。这是本讲最容易被忽略、却最致命的一条工程要求（见 4.2.3 末尾）。

#### 4.1.4 代码实践

**实践目标**：用项目里现成的 `tb_axi_cdc.vhd`，在两个异步时钟域之间跑通一次读事务，确认 AR/R 通道数据正确跨域，并梳理 CDC 模块内部复用了哪些底层实体。

**操作步骤**：

1. 打开 [tb_axi_cdc.vhd:152-192](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L152-L192) 的 `read_block`。这里把一个 `bfm.axi_master`（接 `clk_input`）经 `axi_read_cdc` 接到 `bfm.axi_read_slave`（接 `clk_output`），中间没有任何别的逻辑——正是一个纯净的读路径 CDC 连通性测试。
2. 注意两个时钟 generic：[tb_axi_cdc.vhd:37-39](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L37-L39) 的 `input_clk_fast` / `output_clk_fast` 决定 `clk_input` / `clk_output` 分别用 3 ns（快）还是 7 ns（慢）周期（见 [tb_axi_cdc.vhd:51-52](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L51-L52) 与时钟生成块 [tb_axi_cdc.vhd:82-92](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L82-L92)）。
3. 运行 `test_read` 这个 test case（命令行入口是 `python tools/simulate.py`，用 `--help` 查看「按 testbench / test name 选择」的参数；若参数名记不准，先 `--help` 再选 `tb_axi_cdc` / `test_read`）。配置至少两组 generic：一组「input 快、output 慢」，一组反过来。
4. （可选源码阅读型）把 `num_words` 从 1000 改成 1（[tb_axi_cdc.vhd:49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L49)），在波形里单步观察：master 在 `clk_input` 发出 `ar.valid`，过若干拍 slave 在 `clk_output` 看到 `ar.valid`，再过若干拍 `r.valid` 从 `clk_output` 侧回流到 `clk_input` 侧的 master。

**需要观察的现象**：

- `ar.valid` 在 `clk_output` 侧出现的时间与 `clk_input` 侧不对齐（异步），但内容（`addr`、`len` 等）一字不差。
- `r.valid` / `r.data` 在 `clk_input` 侧被 master 正确采样，`check_bus` 全部通过。

**预期结果**：两种快慢时钟组合下，1000 次随机读全部 `check_bus` 通过，无数据丢失、无死锁（testbench 用 1 ms 的 watchdog 兜底，[tb_axi_cdc.vhd:94](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L94)）。

**列出内部复用链**（对照源码确认）：

```
axi_read_cdc
 ├── axi_address_fifo(asynchronous=true) ─ AR 通道
 │     └── fifo_wrapper(use_asynchronous_fifo=true)
 │           └── asynchronous_fifo
 │                 └── resync_counter   ← 格雷码读写指针跨域（承接 u3-l1 / u4-l2）
 └── axi_r_fifo(asynchronous=true)     ─ R 通道
       └── fifo_wrapper(use_asynchronous_fifo=true)
             └── asynchronous_fifo
                   └── resync_counter
```

也就是说，`axi_read_cdc` 自身**不写任何同步逻辑**，全部 CDC 能力都复用自 `fifo` 模块（`fifo_wrapper` → `asynchronous_fifo` → `resync_counter`）。这也是它没有自己的 `.tcl` 约束文件的根本原因——约束跟着 `asynchronous_fifo` 走。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_read_cdc` 不把 AR 和 R 合并成一个宽度更大的 FIFO？
**答案**：因为 AR 稀疏（每突发 1 拍）、R 密集（每突发多拍），且方向相反（AR 是 input→output，R 是 output→input）。合并会让稀疏地址被迫排在密集数据后头，显著增加地址延迟，也无法各自定制深度/RAM 类型。

**练习 2**：`axi_write_cdc` 实例化了几个通道 FIFO？分别叫什么？
**答案**：三个——`axi_address_fifo`（传 AW）、`axi_w_fifo`（传 W）、`axi_b_fifo`（传 B），全部 `asynchronous => true`。见 [axi_write_cdc.vhd:57-119](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_write_cdc.vhd#L57-L119)。

---

### 4.2 axi_address_fifo：请求通道的记录打包与跨域

#### 4.2.1 概念说明

`axi_address_fifo` 服务于 AR 和 AW 这两条**地址请求**通道（两条通道的信号结构完全一样，所以共用一个实体）。它要做两件事：

1. **打包**：把 record 类型的 `axi_m2s_a_t`（valid、id、addr、len、size、burst 等一堆字段）压成一根尽量窄的 `std_ulogic_vector`，再送进 FIFO。
2. **跨域 / 缓冲**：通过 `asynchronous` generic 选择同步 FIFO 还是异步 FIFO；通过 `depth=0` 选择「不要 FIFO，直接透传」。

为什么必须先打包？因为底层 `fifo_wrapper` 是个**与协议无关**的通用 FIFO，它只认 `write_data : std_ulogic_vector`——不认识 record。所以通道 FIFO 的职责就是「在 record 和扁平向量之间做翻译」。

#### 4.2.2 核心流程

打包的关键原则（承接 u5-l1 的 axi_stream 打包思路）：

- **`valid` 和 `ready` 不进 RAM**。它们是握手控制信号，`valid` 由 FIFO 的 `read_valid` 重新产生，`ready` 直接接 FIFO 的 `write_ready`。把控制位也塞进 RAM 只会白白加宽位宽、浪费资源。
- **只打包实际用到的负载位**。地址 record 里有些字段被项目「精简」掉了（见下文），不打包。
- 打包宽度由一个精化期函数算出来，保证向量正好装下所有负载字段，不多一位。

打包宽度公式（不含 valid）：

\[
w_{AR} = \text{id\_width} + \text{addr\_width} + \text{len\_sz} + \text{size\_sz} + \text{burst\_sz}
\]

#### 4.2.3 源码精读

先看 record 定义，注意那行「被排除成员」注释：

[axi_pkg.vhd:131-140](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L131-L140) —— `axi_m2s_a_t` 包含 valid/id/addr/len/size/burst，注释明确 `lock/cache/prot/region` 这些「几乎不随事务变化」的字段被砍掉了，进一步压缩位宽。打包宽度函数严格对应这些保留字段，**排除 valid**：

[axi_pkg.vhd:439-445](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L439-L445) —— `axi_m2s_a_sz` 返回的就是上面那个 \(w_{AR}\)，注释 `-- Excluded member: valid` 点明控制位不进 RAM。打包函数把字段逐段拼接进 result：

[axi_pkg.vhd:447-478](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L447-L478) —— `to_slv` 用 `lo/hi` 滚动指针依次拼入 id、addr、len、size、burst，末尾 `assert hi = result'high` 守卫「恰好装满、无空洞也无溢出」。`to_axi_m2s_a` 是它的逆运算，把向量拆回 record。

回到通道 FIFO 本身。它先做一个二选一 generate：

[axi_address_fifo.vhd:54-59](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_address_fifo.vhd#L54-L59) —— `depth = 0` 时**直通**：`output_m2s <= input_m2s; input_s2m <= output_s2m;`，连 FIFO 都不实例化，零资源、零延迟。这承接 u4-l2 里 `fifo_wrapper`「`depth=0` 即直通」的同款思路。

否则进入 FIFO 分支，用三个局部信号做翻译：

```vhdl
constant ar_width : positive := axi_m2s_a_sz(id_width=>id_width, addr_width=>addr_width);
signal read_valid : std_ulogic := '0';
signal read_data, write_data : std_ulogic_vector(ar_width - 1 downto 0);
...
assign : process(all)
begin
  write_data <= to_slv(input_m2s, id_width, addr_width);          -- record → 向量，写侧
  output_m2s <= to_axi_m2s_a(read_data, id_width, addr_width);     -- 向量 → record，读侧
  output_m2s.valid <= read_valid;                                  -- valid 由 FIFO 重新产生
end process;
```

见 [axi_address_fifo.vhd:61-77](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_address_fifo.vhd#L61-L77)。注意 `input_s2m.ready` 没有在这段组合逻辑里赋值——它直接来自 FIFO 的 `write_ready` 输出（下一块代码）。

最后是核心的 `fifo_wrapper` 实例化，所有跨域能力都委托给它：

[axi_address_fifo.vhd:80-100](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_address_fifo.vhd#L80-L100) —— 关键三个映射：

- `use_asynchronous_fifo => asynchronous`：一个 generic 在同步 / 异步 FIFO 间切换（承接 u4-l2）。
- `clk_write => clk_input`、`clk_read => clk`：在 `axi_read_cdc` 里 `clk=clk_output`，所以**写在 master 域（`clk_input`）、读在 slave 域（`clk_output`）**，正好是 AR 请求 input→output 的方向。
- `write_ready => input_s2m.ready`、`read_valid => read_valid`：握手控制位由 FIFO 自己产生，不进 RAM。

> 约束提醒（再次强调）：当 `asynchronous=true` 时，本实体产生的真实 CDC 路径在 `asynchronous_fifo` 内部。综合时**必须**把 [asynchronous_fifo.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/scoped_constraints/asynchronous_fifo.tcl) 作用域约束（`read_xdc -ref asynchronous_fifo`）应用到这个 FIFO 实例上，否则时序会因跨域路径报错。这份约束做了两件事：对读数据寄存器设 `set_false_path`（LUTRAM 实现时才出现、可安全忽略的跨域路径，见 [asynchronous_fifo.tcl:32-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/scoped_constraints/asynchronous_fifo.tcl#L32-L49)），并为「LUTRAM 读写潜在冲突」告警开 CDC-26 waiver。

#### 4.2.4 代码实践

**实践目标**：亲手体会「打包宽度随 generic 变化」，并验证 `depth=0` 的直通分支。

**操作步骤**（源码阅读型 + 综合型）：

1. 在 `axi_address_fifo.vhd` 里，把 `id_width`/`addr_width` 设成两组值（例如 `(0, 32)` 与 `(8, 64)`），手算 `axi_m2s_a_sz` 的结果，再对照 [axi_pkg.vhd:439-445](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L439-L445) 验证。
2. 用 `python tools/synthesize.py`（详见 u9-l2）分别对 `axi_address_fifo` 以 `depth=0` 和 `depth=16` 两组综合，比较资源。**预期**：`depth=0` 时 FIFO 实例被 generate 删除，几乎零资源；`depth=16` 时出现一块 RAM（宽度就是上面算出的 \(w_{AR}\)）。
3. 在波形上观察：`depth=0` 时 `output_m2s.valid` 与 `input_m2s.valid` 逐拍完全一致（纯透传），不存在 FIFO 延迟。

**预期结果**：`depth=0` 直通、零延迟；`depth>0` 出现宽度为 \(w_{AR}\) 的 RAM，且 `valid` 由 `read_valid` 在读时钟域重新产生。**待本地验证**：具体资源数取决于 `ram_type` 与目标器件。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `valid` 也打包进 `write_data`，会出什么问题？
**答案**：浪费 RAM 宽度（多 1 位、还可能因对齐多耗 LUT），而且语义上会重复——FIFO 非空就代表「有一拍有效数据」，`read_valid` 已经能表达 valid，再存一份既冗余又容易和 FIFO 的空满状态打架。

**练习 2**：`axi_address_fifo` 的 `clk_input` 端口默认值是 `'0'`（[axi_address_fifo.vhd:40](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_address_fifo.vhd#L40)），为什么敢给默认值？
**答案**：因为只有 `asynchronous=true` 时才需要第二个时钟；同步模式下 `fifo_wrapper` 不使用 `clk_write`，所以给个 `'0'` 默认值能让同步使用者少连一根线，接口更干净。

---

### 4.3 axi_b_fifo：响应通道的反向跨域

#### 4.3.1 概念说明

`axi_b_fifo` 服务于 B 通道（写响应）。它和 `axi_address_fifo` 结构几乎一模一样：同样的 `depth=0` 直通 generate、同样的 record↔slv 翻译、同样的 `fifo_wrapper` 实例化。**唯一的本质区别是方向反了**——B 是 slave 回给 master 的响应，所以数据在 slave（output）侧写入、在 master（input）侧读出。

这一节的价值就在于看清「同一个 FIFO 骨架，怎么通过交换写/读时钟和写/读端口，实现反向跨域」。

#### 4.3.2 核心流程

B 通道的 AXI 语义：slave 产生 `BVALID + BRESP + BID`（这是 slave→master 方向，即 `axi_s2m_b_t`），master 回 `BREADY`（master→slave 方向，即 `axi_m2s_b_t`，只有 `ready` 一个字段）。因此：

- **写侧 = output（slave）域**：把 slave 给的 `output_s2m`（valid/resp/id）打包写入 FIFO，写时钟用 `clk`（= `clk_output`）。
- **读侧 = input（master）域**：从 FIFO 读出数据、还原成 `input_s2m`（valid/resp/id）回给 master，读时钟用 `clk_input`。

对比地址通道（写在 `clk_input`、读在 `clk_output`），B 通道正好把写读时钟**对调**。

#### 4.3.3 源码精读

打包宽度这次用 `axi_s2m_b_sz`（因为 B 的负载是 slave→master 方向的 s2m 字段）：

[axi_b_fifo.vhd:60-66](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_b_fifo.vhd#L60-L66) —— `b_width := axi_s2m_b_sz(id_width)`，定义 `write_data/read_data` 向量。注意这里只传了 `id_width`：B 通道负载只有 `resp`（2 位）加 `id`，没有地址也没有数据，所以很窄。

assign 进程的方向是关键，请对照地址通道阅读：

```vhdl
assign : process(all)
begin
  input_s2m <= to_axi_s2m_b(read_data, id_width);   -- 读出 → 回给 master（input 侧）
  input_s2m.valid <= read_valid;
  write_data <= to_slv(output_s2m, id_width);         -- slave 给的响应 → 打包写入
end process;
```

见 [axi_b_fifo.vhd:69-76](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_b_fifo.vhd#L69-L76)。和地址通道相比，`input_*` 与 `output_*` 的角色对调了：这里 `read_data` 还原到 **input 侧的 s2m**（给 master），`write_data` 来自 **output 侧的 s2m**（slave 产出）。

`fifo_wrapper` 的端口映射同样体现了反向：

[axi_b_fifo.vhd:79-100](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_b_fifo.vhd#L79-L100) —— 注意三处与地址通道的差异：

| 信号 | axi_address_fifo（请求） | axi_b_fifo（响应） |
| --- | --- | --- |
| `clk_write` | `clk_input` | `clk`（=clk_output，slave 域） |
| `clk_read` | `clk` | `clk_input`（master 域） |
| `write_data` 来源 | `input_m2s`（master 请求） | `output_s2m`（slave 响应） |
| `read_data` 去向 | `output_m2s`（给 slave） | `input_s2m`（给 master） |

同一套 `fifo_wrapper`，仅仅交换写/读时钟与写/读数据方向，就从「正向跨域」变成了「反向跨域」。这是本讲最值得记住的设计手法。

#### 4.3.4 代码实践

**实践目标**：在写路径里观察 B 响应的回流方向。

**操作步骤**：

1. 看 [tb_axi_cdc.vhd:196-237](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L196-L237) 的 `write_block`：master 在 `clk_input`、`axi_write_cdc` 跨域、`bfm.axi_write_slave` 在 `clk_output`。
2. 运行 `test_write`（用 `--help` 查选择参数）。testbench 先 `set_expected_words`，再 `write_bus` 写 1000 个随机字，最后 `check_expected_was_written`（[tb_axi_cdc.vhd:118-128](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L118-L128)）。
3. 在波形上跟踪一个 B 响应：它由 slave 在 `clk_output` 发出，经 `axi_b_fifo` 后在 `clk_input` 侧被 master 收到。

**预期结果**：所有写入都被 slave 正确接收，`check_expected_was_written` 通过。注意 `enable_data_fifo_packet_mode => true`（[tb_axi_cdc.vhd:207](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L207)）——W 通道开了 packet 模式（见 4.4）。

#### 4.3.5 小练习与答案

**练习 1**：B 通道打包宽度为什么只取决于 `id_width`，而不需要 `addr_width`/`data_width`？
**答案**：B 响应只携带 `resp`（2 位）和 `id`，不含地址也不含数据（地址在 AW、数据在 W）。所以 `axi_s2m_b_sz` 只算 `resp + id`，宽度只由 `id_width` 决定。

**练习 2**：如果把 `axi_b_fifo` 的 `clk_write`/`clk_read` 接反（写成和地址通道一样），会发生什么？
**答案**：FIFO 会在错误的时钟域采样——slave 在 `clk_output` 给的 `BVALID` 被 `clk_input` 侧当成写时钟去写、master 在 `clk_input` 的 `BREADY` 接到 `clk_output` 读侧，握手和数据都会错位，功能完全错误。方向必须和数据流一致。

---

### 4.4 数据通道 FIFO（R / W）：packet 模式与 level 输出

#### 4.4.1 概念说明

`axi_r_fifo`（读数据）和 `axi_w_fifo`（写数据）处理的是两条**密集**数据通道，所以它们比地址 / 响应 FIFO 多两个能力：

1. **packet 模式**：承接 u4-l1 的 `fifo` 实体，可以要求「整段突发（一个 packet）攒齐后才对外可见」，避免下游看到半个突发。
2. **level 旁路**：`axi_r_fifo` 暴露 `output_level`，告诉外部「R FIFO 里已经攒了几拍」，可供节流器使用。

W 通道的方向和地址通道一致（input→output，master 写、slave 读）；R 通道的方向和 B 通道一致（output→input，slave 写、master 读）。

#### 4.4.2 核心流程

packet 模式的实现完全复用 `fifo` 实体自身的 `enable_last` + `enable_packet_mode`（详见 u4-l1）。通道 FIFO 只需要把这两个 generic 透传下去：

```
enable_packet_mode = true 时:
  fifo_wrapper 额外映射 enable_last  => true   (RAM 字宽 +1，存 last 标记)
  fifo_wrapper 额外映射 enable_packet_mode => true (整包才可见)
  write_last <= input_m2s.last   (W) 或来自 slave 的 last (R)
```

`output_level` 则直接取自 `fifo_wrapper` 的 `write_level` 端口——在异步模式下，这个 level 反映的是**写侧**（对 R 而言就是 slave/output 侧）已经写入但尚未被读走的拍数。

#### 4.4.3 源码精读

先看 `axi_r_fifo` 的 level 端口和注释：

[axi_r_fifo.vhd:38-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_r_fifo.vhd#L38-L50) —— `output_level` 端口，注释明确：「异步 FIFO 时，这个值在 output 侧」。因为 R 的写侧就是 output（slave）域，所以 level 表示 slave 已经回吐但 master 还没取走的数据量。它正是 `axi_read_cdc` 顶层 `output_data_fifo_level` 的来源（[axi_read_cdc.vhd:48](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_cdc.vhd#L48)）。

packet 模式与 level 的透传：

[axi_r_fifo.vhd:84-107](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_r_fifo.vhd#L84-L107) —— 关键映射：`enable_last => enable_packet_mode`、`enable_packet_mode => enable_packet_mode`（两个都跟随同一个 generic），以及 `write_level => output_level`。注意 `clk_write => clk`、`clk_read => clk_input`，与 B 通道同属「响应方向」（写在 output 域、读在 input 域）。

W 通道结构与 R 对称，方向相反（写在 input 域、读在 output 域），并且把 `write_last` 也接上：

[axi_w_fifo.vhd:84-106](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_w_fifo.vhd#L84-L106) —— `enable_last => enable_packet_mode`、`enable_packet_mode => enable_packet_mode`、`write_last => input_m2s.last`（master 标记的包尾）。`clk_write => clk_input`、`clk_read => clk`，与地址通道同属「请求方向」。

> 一个一致性检查：四个数据/地址通道 FIFO 里，「请求方向」的 AR/AW/W 都是 `clk_write=>clk_input, clk_read=>clk`；「响应方向」的 R/B 都是 `clk_write=>clk, clk_read=>clk_input`。记住这条规律，读任何一个通道 FIFO 都不会搞反方向。

#### 4.4.4 代码实践

**实践目标**：体会 packet 模式对「整包可见性」的影响。

**操作步骤**：

1. 在 [tb_axi_cdc.vhd:163](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L163) 把读路径的 `enable_data_fifo_packet_mode` 改成 `true`（原值是 `false`），重新跑 `test_read`。
2. 对照 [tb_axi_cdc.vhd:207](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L207)，写路径本来就开了 packet 模式。
3. 在波形上观察：开 packet 模式后，R FIFO 在收到完整突发（`last` 到达）前，master 侧的 `r.valid` 不会拉高；关掉时则逐拍可见。

**预期结果**：两种模式下功能都正确（`check_bus` 通过），但 `r.valid` 的时序形态不同。**待本地验证**：packet 模式会额外消耗 `num_lasts_in_fifo` 计数逻辑（见 u4-l1），资源略增。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `axi_r_fifo` 的 `output_level`「在异步模式下是 output 侧的值」？
**答案**：因为 R 通道写侧在 output（slave）域，`fifo_wrapper` 的 `write_level` 天然反映写侧已写入未读走的拍数。异步 FIFO 的读侧在另一个时钟域，无法直接给出确定性的 level（指针跨域后是陈旧值，见 u4-l2 的「方向性安全」），所以只能提供写侧 level。

**练习 2**：`axi_w_fifo` 把 `enable_last` 和 `enable_packet_mode` 都接成 `enable_packet_mode`，为什么不分开成两个 generic？
**答案**：因为 packet 模式必然依赖 `last` 标记来界定包尾（u4-l1 里 `enable_packet_mode` 依赖 `enable_last`），两者在这个场景下逻辑绑死。合并成一个 generic 简化了外部接口，也避免了「开 packet 却关 last」这种自相矛盾的配置。

---

## 5. 综合实践

把本讲内容串起来，做一个「读 + 写双路径 CDC」的小工程：

1. **搭建**：仿照 [tb_axi_cdc.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd)，写一个最小 testbench：一个 `bfm.axi_master` 跑 `clk_input`，经 `axi_read_cdc` + `axi_write_cdc` 接到 `clk_output` 域的 `bfm.axi_read_slave` / `bfm.axi_write_slave`（可直接复用 testbench 里的 `memory` 模型）。
2. **配置**：让 `clk_input` 与 `clk_output` 一个用 3 ns、一个用 7 ns；给数据 FIFO 设较大的 `data_fifo_depth`（如 1024），给地址 / 响应 FIFO 设较小的深度（如 32），体会「按通道流量定制深度」。
3. **跑混合事务**：先发若干写事务填一片地址区，再发读事务回读校验，确认跨域后数据一致。
4. **加扰动**：把 slave 的 `*_stall_probability` 调高（见 [tb_axi_cdc.vhd:72-74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_cdc.vhd#L72-L74)），验证在重背压下 CDC 仍不丢数据。
5. **画复用链**：在报告里画出 `axi_read_cdc` / `axi_write_cdc` → 各通道 FIFO → `fifo_wrapper` → `asynchronous_fifo` → `resync_counter` 的完整实例化树，并标注每个通道 FIFO 的写 / 读时钟分别属于哪个时钟域、属于「请求方向」还是「响应方向」。

**验收标准**：所有读写校验通过；能准确说出五条通道各自的跨域方向与内部复用的底层实体；能指出综合时需要套用的约束文件（`asynchronous_fifo.tcl`）。

---

## 6. 本讲小结

- AXI 跨时钟域的核心策略是**按通道拆 FIFO**：AR / R / AW / W / B 五条独立通道各挂一个异步 FIFO，深度和 RAM 类型按各通道流量单独定制，地址路径与数据路径彻底解耦。
- `axi_read_cdc` / `axi_write_cdc` 只是「顶层接线员」：把每条通道的 record 端口接到对应的通道 FIFO，并把 `clk_input` / `clk_output` 两个时钟分配下去；自身不写任何同步逻辑。
- 每个通道 FIFO 用 `to_slv` / `to_axi_*` 在 record 与扁平向量间翻译，**只打包负载位、排除 `valid`/`ready`**（控制位由 FIFO 的 `read_valid` / `write_ready` 重新产生），从而把 FIFO 宽度压到最窄。
- 请求通道（AR / AW / W，input→output）与响应通道（R / B，output→input）方向相反，靠**交换 `fifo_wrapper` 的写 / 读时钟与写 / 读数据方向**用同一套骨架实现两种流向。
- `depth=0` 触发直通 generate、`asynchronous` 切换同步 / 异步——两个 generic 让一个实体覆盖「透传 / 同步缓冲 / 异步 CDC」三种用法。
- 全部 CDC 能力都复用自 `fifo` 模块（`fifo_wrapper` → `asynchronous_fifo` → `resync_counter`），因此 `axi` 模块没有自己的约束文件，综合时必须套用 `fifo.asynchronous_fifo` 的作用域约束。

---

## 7. 下一步学习建议

- **向「轻量总线」收束**：本讲处理的是完整 AXI4。如果你的控制平面只是配寄存器，下一讲 u5-l4 会讲 AXI-Lite 子系统（`axi_lite_mux` / `axi_lite_cdc` / `axi_to_axi_lite`），其中的 `axi_lite_cdc` 是「Lite 版的通道 CDC」，可以和本讲对照阅读。
- **向「使用方」延伸**：u7-l2 的 DMA（`dma_axi_write_simple`）会把 AXI-Stream 数据经 AXI 写通道打进 DDR，其内部就会用到本讲这类 AXI 通道 FIFO / CDC。学完 DMA 你会看到这套 CDC 组件在一个真实 IP 里如何被组装。
- **吃透底层**：如果想真正理解「为什么异步 FIFO 的指针能安全跨域」，回头精读 u3-l1（`resync_counter` 的格雷码）和 u4-l2（`asynchronous_fifo` 的方向性安全与约束），本讲的 `fifo_wrapper` → `asynchronous_fifo` 那一层就完全透明了。
- **验证视角**：u8-l1 会系统讲 master / slave BFM 怎么施加随机背压——本讲 testbench 里的 `stall_probability` 正是那套方法论的具体应用。
