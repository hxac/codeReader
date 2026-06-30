# axi_test：底层驱动

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `axi_test` 包里 `axi_lite_driver` 与 `axi_driver` 两个类**是什么、解决什么问题**——它们把「在一个 AXI 通道上完成一次合法握手」这件容易写错的事，封装成 `send_aw` / `recv_b` 这样的任务（task）。
- 看懂 `TA`（stimuli application time）与 `TT`（stimuli test time）这两个时序参数：为什么驱动信号要用 `<= #TA` 延迟赋值、为什么采样要 `#TT` 之后再读。
- 掌握 `reset_master` / `reset_slave` 的初始化套路，并能解释为什么复位要按 modport 的「输出」方向清零。
- 理解 driver 如何通过**虚接口** `virtual AXI_LITE_DV` / `AXI_BUS_DV` 绑定到测试台里的具体接口实例上。
- 用 `axi_lite_driver` 手写出一次完整的「32 位写 + 32 位读 + 检查返回数据」过程。

本讲只讲**底层逐拍驱动器**。更高层的随机主从（`axi_rand_master` / `axi_rand_slave`）、scoreboard、`axi_sim_mem` 留给 u3-l2；完整测试台的骨架留给 u3-l3。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自 u1-l3、u2-l3、u2-l4）：

- **五通道与握手**：写事务走 AW→W→B，读事务走 AR→R；`valid` 与 `ready` 在**同一个时钟上升沿同时为高**才算发生一次握手（一个 beat）。`valid` 一旦拉高，在握手发生前**不能撤回**——这是 AXI 的铁律。
- **in flight（在途）与 pending（挂起）**：地址拍已握手但响应拍还没握手，叫 in flight（即 outstanding）；`valid` 高而 `ready` 低、正等待握手，叫 pending。
- **SystemVerilog interface / modport**：`AXI_BUS` 与 `AXI_LITE` 把约 43 根（或 Lite 的十几根）信号打包成一个 bundle，并用 `Master` / `Slave` / `Monitor` 三个 modport 预设每根信号的方向（`input`/`output`）。其中 `AXI_LITE_DV` / `AXI_BUS_DV` 只是多了一个 `clk_i` 输入端口（见 [src/axi_intf.sv:474-533](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L474-L533)），有了时钟才能在接口里写 `assert property` 断言、才能让 driver 任务里的 `@(posedge clk_i)` 工作。
- **prot_t / resp_t 类型**：都来自 `axi_pkg`。`prot_t` 是 3 位的保护位（见 [src/axi_pkg.sv:54](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L54)，对应 `ProtWidth = 3` 见 [src/axi_pkg.sv:31](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L31)）；`resp_t` 是 2 位响应码（OKAY/EXOKAY/SLVERR/DECERR）。

如果你对 SystemVerilog 的 `class`、`task`、非阻塞赋值 `<= #delay`（带内建延迟的非阻塞赋值）、`virtual interface` 这些语法还不熟，下面会随讲随解释。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/axi_test.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv) | 全库唯一的验证用 `package`。本讲只读它的开头部分：`axi_lite_driver`（[L26-L224](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L26-L224)）、四个 beat 类（[L228-L279](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L228-L279)）、`axi_driver`（[L283-L680](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L283-L680)）。 |
| [test/tb_axi_lite_to_axi.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv) | 真实测试台，**本讲最重要的范本**。它同时例化了 `axi_lite_driver` 和 `axi_driver`，并展示了 `reset_*` → `send_*` → `recv_*` 的完整调用顺序。 |
| [src/axi_intf.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv) | 提供 driver 绑定用的 `AXI_LITE_DV` / `AXI_BUS_DV` 接口及 Master/Slave modport。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 提供 `prot_t` / `resp_t` 等 driver 任务里用到的类型。 |

## 4. 核心概念与源码讲解

### 4.1 虚接口绑定、构造与复位套路

#### 4.1.1 概念说明

测试台里驱动 AXI 总线，最朴素的写法是直接对接口信号做赋值：

```systemverilog
axi_lite.aw_addr  = 32'h1000;
axi_lite.aw_valid = 1'b1;
@(posedge clk);
// ... 然后还要判断 ready、还要记得把 valid 拉回去 ...
```

这样写有两个毛病：一是**容易违反协议**（比如忘了「valid 在握手前不能撤」、忘了握手后要复位）；二是**重复**——每个通道、每个测试台都要抄一遍。

`axi_lite_driver` / `axi_driver` 就是为了消除这两点而生的 `class`。它把一个通道上「驱动一次合法握手」封装成 `send_aw`、`send_w`、`recv_b` 这样的 `task`。你只要告诉它**驱动哪个接口**（通过虚接口绑定），它就替你处理时序、握手、清零。

关键机制是 **virtual interface（虚接口）**：SystemVerilog 里，`class`（属于「动态」、跑在 `initial`/`process` 里）默认**不能直接访问** `interface` 实例（属于「静态」、硬件拓扑）。于是用 `virtual AXI_LITE_DV` 这个句柄把一个具体接口实例「递」进类里，类内通过这个句柄间接驱动信号。这就是构造函数 `new()` 唯一的活儿。

#### 4.1.2 核心流程

```
测试台侧：
  1. 声明 AXI_LITE_DV #(...)  axi_lite_dv(clk);   // 带时钟的接口实例
  2. 构造 driver：drv = new(axi_lite_dv);          // 把实例句柄传进类
  3. drv.reset_master();                            // 复位本侧所有输出
  4. @(posedge clk);                                // 对齐到时钟边沿
  5. drv.send_aw(addr, prot); ...                   // 调任务驱动

类内：
  new(virtual AXI_LITE_DV axi)  ->  this.axi = axi;   // 仅保存句柄
  reset_master()  ->  把 Master modport 的所有 output 驱为 '0
  reset_slave()   ->  把 Slave  modport 的所有 output 驱为 '0
```

#### 4.1.3 源码精读

`axi_lite_driver` 的参数与字段（[src/axi_test.sv:26-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L26-L35)）：`AW`/`DW` 是地址/数据位宽，`TA`/`TT` 是时序参数（下一节专讲），字段 `axi` 就是那个虚接口句柄。

构造函数只做一件事——把传入的虚接口存起来（[src/axi_test.sv:37-44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L37-L44)）：

```systemverilog
function new(virtual AXI_LITE_DV #(.AXI_ADDR_WIDTH(AW), .AXI_DATA_WIDTH(DW)) axi);
  this.axi = axi;
endfunction
```

`reset_master()` 把 Master 侧该驱动的信号全部清零（[src/axi_test.sv:46-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L46-L58)）。注意它清的是 `aw_valid`、`w_valid`、`b_ready`、`ar_valid`、`r_ready` 这些**输出方向**的信号——对照 [AXI_LITE_DV 的 Master modport](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L512-L518)，`output` 的正是这些；`aw_addr`/`aw_prot`/`w_data`/`w_strb`/`ar_addr`/`ar_prot` 这些输出载荷也被顺手清零。`reset_slave()`（[src/axi_test.sv:60-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L60-L69)）则对称地清 Slave 侧的 `aw_ready`/`w_ready`/`b_valid`/`b_resp`/`ar_ready`/`r_valid`/`r_data`/`r_resp`。

真实例化范本见 [test/tb_axi_lite_to_axi.sv:68-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L68-L69)：

```systemverilog
axi_test::axi_lite_driver #(.AW(TB_AW), .DW(TB_DW)) axi_lite_drv = new(axi_lite_dv);
axi_test::axi_driver #(.AW(TB_AW), .DW(TB_DW), .IW(TB_IW), .UW(TB_UW)) axi_drv = new(axi_dv);
```

一行就完成了「参数化 + 绑定虚接口 + 构造」。`#(.AW, .DW)` 把位宽传进类，`new(axi_lite_dv)` 把测试台里的 `axi_lite_dv` 实例（[tb_axi_lite_to_axi.sv:31-34](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L31-L34)）交给 driver。

#### 4.1.4 代码实践

**目标**：体会「构造即绑定」与 `reset_*` 的方向性。

1. 打开 `test/tb_axi_lite_to_axi.sv`，定位到 [L68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L68) 的例化行。
2. 把 `axi_lite_drv` 绑定的虚接口从 `axi_lite_dv` 改成 `axi_dv`（一个 `AXI_BUS_DV`，类型不匹配）。
3. 重新编译（`make compile.log`）。
4. **预期**：编译器报类型不匹配错误——`axi_lite_driver` 的构造函数要求 `virtual AXI_LITE_DV`，传一个 `AXI_BUS_DV` 无法绑定。这说明绑定是**类型严格**的：Lite driver 只能绑 Lite 接口，完整 AXI driver 只能绑 `AXI_BUS_DV`。（具体报错文本因仿真器而异，待本地验证。）
5. 改回原样。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `reset_master()` 要清 `b_ready` 和 `r_ready`，却不清 `b_resp` 和 `r_data`？

**答案**：在 Master modport 里，`b_ready`/`r_ready` 是 Master 的**输出**（Master 驱动它们），所以复位时必须由 Master 侧清零；而 `b_resp`/`r_data` 是 Master 的**输入**（Slave 驱动它们），Master 没有资格、也没必要去驱动它们——那是 `reset_slave()` 的活儿。复位严格按 modport 方向分工。

**练习 2**：`new()` 里只有一行 `this.axi = axi;`，为什么不在这里顺便把信号清零？

**答案**：构造只做「记住我绑定了哪个接口」。清零属于「复位」语义，应该等真正进入复位阶段（通常配合 `rst_n`）再做，所以单独提供 `reset_master()`/`reset_slave()` 供测试台在合适时机调用。把职责拆开，driver 才能被反复 `reset()` 而不必重新构造。

---

### 4.2 TA 与 TT：application time 与 test time

#### 4.2.1 概念说明

`TA`（stimuli application time，激励施加时间）和 `TT`（stimuli test time，激励检测时间）是本驱动的**时序灵魂**，也是初学者最容易看不懂的地方。

真实硬件里，一个寄存器的 Q 端在时钟上升沿之后，要经过 **clock-to-Q 延迟**才会更新；而下级触发器要求输入在**下一个上升沿之前**（setup time）就稳定。driver 要忠实模拟这种行为，否则仿真出来的时序会比真实硬件「过于理想」，掩盖真实 bug。

本驱动的约定是：

- **TA**：在时钟上升沿**之后**延迟 TA 时间，再去驱动（施加）本侧的输出信号。对应代码里的 `<= #TA value`。
- **TT**：在时钟上升沿**之后**延迟 TT 时间，再去采样（检测）对侧驱动过来的输入信号。对应代码里的 `#TT`。

通常取 \( 0 \le TA < TT < T_{\text{clk}} \)，并且 TA 与 TT 都明显小于半个时钟周期。这样驱动出来的波形：本侧输出在沿后 TA 处跳变、对侧输入在沿后 TT 处被读取，两者都远离上升沿，既模拟了 clock-to-Q，又满足了 setup/hold 直觉。例如 `axi_lite_rand_master` 默认 `TA = 2ns, TT = 8ns`（[src/axi_test.sv:1558-1560](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1558-L1560)）。

#### 4.2.2 核心流程

驱动器内部用两个极小的任务把时序封装起来（[src/axi_test.sv:71-77](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L71-L77)）：

```
cycle_start();  =>  #TT;             // 走到「沿 + TT」，准备采样对侧输入
cycle_end();    =>  @(posedge clk_i); // 等到下一个上升沿
```

一个 `send_*` 任务的时序骨架因此是：

```
在某个上升沿 Tn 进入任务
  用 <= #TA 把输出(载荷 + valid)排定到 Tn + TA   // 沿后 TA 施加激励
  cycle_start();                                // 到 Tn + TT，可以读对侧 ready
  while (对侧 ready != 1):                       // 没握手就等下一拍
      cycle_end();  -> Tn+1 沿
      cycle_start();-> Tn+1 + TT
  cycle_end();   -> 握手发生那一拍的下一沿 Tk+1
  用 <= #TA 把输出清零到 Tk+1 + TA              // 撤激励
```

`recv_*` 任务对称：用 `<= #TA` 驱动本侧 `*_ready`，用 `#TT`(cycle_start) 之后采样对侧 `*_valid`。

#### 4.2.3 源码精读

时序封装（[src/axi_test.sv:71-77](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L71-L77)）：

```systemverilog
task cycle_start;  #TT;                endtask   // 沿后 TT：采样窗口
task cycle_end;    @(posedge axi.clk_i); endtask  // 推进到下一沿
```

`<= #TA` 的典型用法，以 `send_aw` 开头三行（[src/axi_test.sv:84-86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L84-L86)）：

```systemverilog
axi.aw_addr  <= #TA addr;   // 载荷：沿后 TA 施加
axi.aw_prot  <= #TA prot;
axi.aw_valid <= #TA 1;      // valid：沿后 TA 拉高
```

注意是**非阻塞赋值 `<=`** 带**内建延迟 `#TA`**：它把「TA 之后赋值」排进调度，不阻塞当前任务继续往下走到 `cycle_start()`。这正对应「时钟沿触发寄存器、clock-to-Q 后输出才变」的物理含义。

参数声明处的注释也点明了语义（[src/axi_test.sv:29-30](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L29-L30)）：`TA // stimuli application time`、`TT // stimuli test time`。

#### 4.2.4 代码实践

**目标**：直观看到 TA/TT 对波形的影响。

1. 找一个使用 `axi_lite_driver` 且 `TA`/`TT` 非零的测试台（或仿照 [tb_axi_lite_to_axi.sv:85-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L85-L95) 写一个最小 TB），例化时显式传 `#(.TA(2ns), .TT(8ns))`。
2. 在仿真器里把 `axi_lite_dv.aw_valid`、`clk` 加进波形窗口。
3. **观察**：`aw_valid` 的上升沿应出现在 `clk` 上升沿之后约 2ns（TA），而不是与 `clk` 对齐。
4. 把 `TA` 改成 `0ns` 重跑，对比 `aw_valid` 是否变得与 `clk` 对齐。
5. **预期**：TA>0 时输出相对时钟沿有可见延迟，模拟了 clock-to-Q；TA=0 时输出与沿对齐。具体延迟数值待本地验证，但「TA 控制施加延迟」这一趋势是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么采样要放在 `#TT`（TT 通常比 TA 大）之后，而不是紧贴上升沿？

**答案**：刚到上升沿时，对侧刚驱动的信号可能还在 clock-to-Q 过渡中（沿后约 TA 才稳定）。若紧贴沿采样，可能采到过渡中的旧值。延迟到 TT（且 TT > TA）再采样，能确保对侧在沿后 TA 施加的激励已经稳定，采到的是本拍真正的有效值。

**练习 2**：如果时钟周期是 10ns，把 `TT` 设成 12ns 会怎样？

**答案**：`TT` 超过一个时钟周期后，`#TT` 会跨到下一拍的区间，`cycle_start()` 之后的采样与 `cycle_end()` 的 `@(posedge clk)` 时序关系会错乱，握手判定可能错拍。所以约定 \( TT < T_{\text{clk}} \)，通常远小于半周期。

---

### 4.3 send_* 与 recv_*：逐拍握手驱动

#### 4.3.1 概念说明

有了绑定与 TA/TT，剩下的就是「在一个通道上完成一次合法握手」。driver 给每个通道提供**两类**任务：

- **`send_*`（主动发）**：本侧作为发起方，把载荷与 `valid` 驱出去，**等对侧 `ready`** 握手后撤销。
- **`recv_*`（被动收）**：本侧把 `ready` 驱出去，**等对侧 `valid`** 握手，并把对侧送来的载荷采样回来。

二者结构完全对称，只是把 `valid`/`ready`、施加/采样对调。`axi_lite_driver` 给五个通道（AW/W/B/AR/R）各提供一对，共 10 个任务（[src/axi_test.sv:80-222](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L80-L222)）。`send_b`/`send_r` 是给「slave 一侧的 driver」用的——Slave 才发 B 和 R。

#### 4.3.2 核心流程

`send_aw(addr, prot)` 的握手流程（[src/axi_test.sv:80-93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L80-L93)）：

```
驱动 aw_addr/aw_prot <= #TA；aw_valid <= #TA 1
cycle_start()  // 到采样窗口
while (aw_ready != 1):           // 没拿到 ready 就持续等
    cycle_end(); cycle_start();  // valid 始终保持高 —— 满足「valid 不撤」铁律
cycle_end()    // 越过握手沿
aw_addr/aw_prot <= #TA '0；aw_valid <= #TA 0   // 握手后才撤销
```

`recv_b(resp)` 的握手流程（[src/axi_test.sv:185-194](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L185-L194)）：

```
b_ready <= #TA 1                  // 告诉对侧「我准备好了」
cycle_start()
while (b_valid != 1):             // 等对侧把 B 送来
    cycle_end(); cycle_start()
resp = axi.b_resp                 // 采样载荷（此时在 TT 窗口，值已稳定）
cycle_end()
b_ready <= #TA 0
```

#### 4.3.3 源码精读

`send_aw` 全文（[src/axi_test.sv:80-93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L80-L93)）：

```systemverilog
task send_aw(input logic [AW-1:0] addr, input prot_t prot);
  axi.aw_addr  <= #TA addr;
  axi.aw_prot  <= #TA prot;
  axi.aw_valid <= #TA 1;
  cycle_start();
  while (axi.aw_ready != 1) begin cycle_end(); cycle_start(); end
  cycle_end();
  axi.aw_addr  <= #TA '0;
  axi.aw_prot  <= #TA '0;
  axi.aw_valid <= #TA 0;
endtask
```

关键点：`while (aw_ready != 1)` 这一圈里，`aw_valid` 自始至终保持高、`aw_addr/aw_prot` 也不变——这正是 AXI「valid 在握手前不可撤、pending 期间载荷须稳定」的铁律，被 driver 自动满足。`send_w(data, strb)`（[src/axi_test.sv:96-109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L96-L109)）、`send_ar(addr, prot)`（[src/axi_test.sv:125-138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L125-L138)）结构完全一致。

真实调用顺序见 `axi_lite_rand_master.write`（[src/axi_test.sv:1698-1709](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1698-L1709)）：一次写 = `fork send_aw ∥ send_w; join` 然后 `recv_b`。`fork…join` 让 AW 与 W 并行发出（协议允许两者同时握手），再收 B 响应。对应的 `.read`（[src/axi_test.sv:1712-1720](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1712-L1720)）则是 `send_ar` 后 `recv_r`。

最干净的实战范本是 [test/tb_axi_lite_to_axi.sv:85-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L85-L95)：

```systemverilog
axi_lite_drv.reset_master();
@(posedge clk);
axi_lite_drv.send_aw('hdeadbeef, axi_pkg::prot_t'('0));
axi_lite_drv.send_w ('hdeadbeef, '1);
axi_lite_drv.recv_b (resp);
$info("AXI-Lite B: resp %h", resp);
```

#### 4.3.4 代码实践

**目标**：亲手用底层任务发起一次 AXI-Lite 写。

1. 打开 `test/tb_axi_lite_to_axi.sv`，阅读 [L85-L95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L85-L95) 的 master initial 块和 [L97-L108](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L97-L108) 的 slave initial 块。后者用 `axi_drv.recv_aw`/`recv_w`/`send_b` 扮演从端。
2. 运行 `make sim-axi_lite_to_axi.log`（或 `scripts/run_vsim.sh tb_axi_lite_to_axi`）。
3. **观察**：日志应打印 `AXI-Lite B: resp 0`（OKAY）以及 slave 侧 `AXI AW: addr deadbeef`、`AXI W: data deadbeef, strb f`。
4. 把 master 块里的 `send_aw` 地址改成 `'hcafebabe`，重跑，确认 slave 侧打印的 addr 随之变化。
5. **预期**：握手成功，B 通道返回 `RESP_OKAY`；slave 打印的地址随你改的值变化。具体打印文本待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`send_aw` 里那个 `while (aw_ready != 1)` 循环，删掉直接写 `cycle_end()` 行不行？

**答案**：不行。删掉就等于假设「我一拉 valid，对侧立刻 ready」，这是 AXI 不允许的假设。真实从端可能要等若干拍才置 ready。该循环正是用来「valid 保持高、逐拍等 ready」的，它保证了无论对侧何时 ready，握手都合法。删掉会在对侧延迟 ready 时错过握手、甚至提前撤 valid 违反协议。

**练习 2**：为什么 `recv_b` 是在 `while` 循环退出**之后**才 `resp = axi.b_resp` 采样，而不是在循环里采？

**答案**：采样必须等到 `b_valid == 1` 那一拍，且落在 TT 采样窗口（`cycle_start` 之后）才可靠。循环里 `b_valid` 还没到 1，采到的是无意义值。退出循环意味着已检测到 `b_valid==1` 且处于 TT 窗口，此刻 `b_resp` 才是本次事务真正的响应，此时采样才正确。

---

### 4.4 axi_driver 的 beat 对象与 monitor 被动监听

#### 4.4.1 概念说明

`axi_lite_driver` 的载荷是「散装参数」（`addr`、`prot`、`data`、`strb`）。完整 AXI4 一个通道有十几根信号（id/addr/len/size/burst/lock/cache/prot/qos/region/atop/user），再用散装参数就太长了。于是 `axi_driver` 改用 **beat 对象**：把一拍的所有载荷打包成一个 `class`（`axi_ax_beat`、`axi_w_beat`、`axi_b_beat`、`axi_r_beat`），任务签名变成 `send_aw(beat)` / `recv_aw(beat)`，一个参数搞定。

另外，`axi_driver` 还多了一组 **`mon_*`（monitor）任务**。`send_*`/`recv_*` 是「主动」的——它们会驱动本侧的 `valid` 或 `ready`；而 `mon_*` 是「被动」的——它**不驱动任何信号**，只旁观 `valid && ready` 同时为高的那一拍，把载荷抄走。这正是 u2-l3 里 `Monitor` modport（只读所有信号）的用武之地，用于做总线监视器/scoreboard。

#### 4.4.2 核心流程

beat 类的构造与字段（以 AW/AR 共用的 `axi_ax_beat` 为例，[src/axi_test.sv:228-245](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L228-L245)）：字段就是 AXI4 AW/AR 通道的全部信号，其中 `ax_atop`「只在 AW 通道定义」。`axi_driver` 内部为每个 beat 类做 `typedef` 别名（[src/axi_test.sv:298-301](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L298-L301)），任务里直接读写 `beat.ax_id`、`beat.ax_addr` 等。

`axi_driver.send_aw(beat)`（[src/axi_test.sv:374-406](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L374-L406)）的结构和 lite 版一模一样，只是把「驱动 addr/prot」扩成「驱动 12 个字段」。

`recv_*`/`mon_*` 因为要「抄走」一整拍，会先 `beat = new;`（新建对象避免别名）再逐字段赋值。

#### 4.4.3 源码精读

beat 类带 `rand` 修饰（[src/axi_test.sv:228-245](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L228-L245)）：

```systemverilog
class axi_ax_beat #(parameter int unsigned AW=32, IW=8, UW=1);
  rand logic [IW-1:0] ax_id = '0;
  rand logic [AW-1:0] ax_addr = '0;
  logic [7:0]         ax_len = '0;
  logic [2:0]         ax_size = '0;
  logic [1:0]         ax_burst = '0;
  ...
  logic [5:0]         ax_atop = '0; // Only defined on the AW channel.
  rand logic [UW-1:0] ax_user = '0;
endclass
```

`rand` 字段（id/addr/qos/user）可被 `.randomize()` 随机化——这是 u3-l2 随机主从的基础。`ax_len/ax_size/ax_burst/ax_atop` 等不带 `rand`，由调用方显式设定。

`axi_driver.send_aw` 开头（[src/axi_test.sv:377-389](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L377-L389)）：逐字段 `<= #TA beat.xxx`，结构同 lite，只是字段更多。`recv_ar` 里有个细节（[src/axi_test.sv:571](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L571)）：`beat.ax_atop = 'X;` 并注释 `// Not defined on the AR channel.`——因为 atop 只在 AW 上有意义，收 AR 时填 X 以提醒使用者别误用。

`mon_aw`（[src/axi_test.sv:595-614](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L595-L614)）和 `recv_aw` 的关键区别：

```systemverilog
// mon_aw: 不驱动 ready，只等 valid && ready 同时为高
cycle_start();
while (!(axi.aw_valid && axi.aw_ready)) begin cycle_end(); cycle_start(); end
beat = new; beat.ax_id = axi.aw_id; ...   // 抄走载荷
```

它**不**像 `recv_aw` 那样 `axi.aw_ready <= #TA 1`——monitor 不参与握手，纯旁观。

真实使用见 [test/tb_axi_lite_to_axi.sv:97-108](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L97-L108)：slave 一侧 `axi_drv.reset_slave()` 后用 `recv_aw(ax_beat)` 收地址、`recv_w(w_beat)` 收数据、`send_b(b_beat)` 回响应。beat 对象在 [L98-L100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L98-L100) 声明，`b_beat = new` 已构造好直接发，`ax_beat`/`w_beat` 由 `recv_*` 内部 `new`。

#### 4.4.4 代码实践

**目标**：用 beat 对象完整驱动一次 AXI4（非 Lite）写，对比它与 Lite 的区别。

1. 阅读 `test/tb_axi_lite_to_axi.sv` 的 [L97-L108](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L97-L108)，看 slave 侧如何用 `ax_beat`/`w_beat` 接收。
2. 在 slave 块的 `recv_aw(ax_beat)` 之后加一行打印（**示例代码**）：
   ```systemverilog
   $info("AW burst: len %0d size %0d burst %0d", ax_beat.ax_len, ax_beat.ax_size, ax_beat.ax_burst);
   ```
3. 重跑仿真。
4. **预期**：因为 master 侧（`axi_lite_drv`）发的是 Lite 写，桥到 AXI4 侧 len=0、size 由数据宽度决定、burst=INCR(1)，打印应反映这些单拍事务特征。具体数值待本地验证。
5. 对比：若直接用 `axi_drv.send_aw` 发一个 `ax_len=3` 的 4 拍突发，slave 侧 `recv_aw` 抄到的 `ax_len` 应为 3——这是 Lite 接口（没有 len/burst）做不到的。

#### 4.4.5 小练习与答案

**练习 1**：`mon_aw` 和 `recv_aw` 都能拿到一个 `ax_beat`，本质区别是什么？什么时候用哪个？

**答案**：`recv_aw` 是**主动**的：它驱动 `aw_ready`、参与握手、会「消费」这一拍（与对侧形成真实握手）。`mon_aw` 是**被动**的：不驱动任何信号，只在 `valid && ready` 同时为高时旁观抄录，不影响总线。当你需要**充当从端**响应事务时用 `recv_*`；当你只是想**旁路监视**总线流量（如 scoreboard、覆盖率采集）时用 `mon_*`，后者绝不会干扰被测对象的行为。

**练习 2**：为什么 `recv_*`/`mon_*` 里都要写一句 `beat = new;` 再赋字段，而不是直接用外部传进来的对象？

**答案**：SystemVerilog 的 `class` 是句柄（引用）语义。若不 `new` 一个新对象而直接赋值到外部句柄，多个调用会共享同一对象，后一次赋值会覆盖前一次，造成数据串扰（别名问题）。`beat = new;` 保证每次收/监听都拿到一个独立的新对象，互不影响。`send_*` 不需要这步，因为它只「读」beat 的字段去驱动信号，不改 beat。

---

## 5. 综合实践

把本讲三件事——**绑定虚接口、用 TA/TT 逐拍握手、send/recv 完成一次写+读**——串起来。

**任务**：仿照 `test/tb_axi_lite_to_axi.sv` 的双 initial 结构，写一个**最小回环（loopback）测试台**：一个 `axi_lite_driver` 当 master 发起「写 0x0000=0xCAFE → 读 0x0000」，另一个 `axi_lite_driver` 当 slave 把写进来的数据存住、读时原样回送，最后 master 检查读回值。

下面是**示例代码**（基于本讲讲解的 API 与 [tb_axi_lite_to_axi.sv:85-108](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L85-L108) 的风格，不是仓库已有文件）：

```systemverilog
// --- 示例代码：最小 AXI-Lite 回环 TB 片段 ---
// 假设已有：clk 生成、AXI_LITE_DV #(.AXI_ADDR_WIDTH(32), .AXI_DATA_WIDTH(32)) lite_dv(clk);

axi_test::axi_lite_driver #(.AW(32), .DW(32), .TA(2ns), .TT(8ns)) m_drv = new(lite_dv);
axi_test::axi_lite_driver #(.AW(32), .DW(32), .TA(2ns), .TT(8ns)) s_drv = new(lite_dv);

logic [31:0] back_store;   // slave 侧「寄存器」

initial begin   // master
  automatic logic [31:0] rdata;
  automatic axi_pkg::resp_t resp;
  m_drv.reset_master();
  @(posedge lite_dv.clk_i);
  // 一次写：AW 与 W 并行发，再收 B
  fork
    m_drv.send_aw(32'h0000, axi_pkg::prot_t'('0));
    m_drv.send_w (32'hCAFE, 32'hF);     // strb=0xF 全字节写
  join
  m_drv.recv_b(resp);
  // 一次读：发 AR，收 R
  m_drv.send_ar(32'h0000, axi_pkg::prot_t'('0));
  m_drv.recv_r(rdata, resp);
  $info("read back: %h", rdata);
  assert (rdata == 32'hCAFE) else $error("data mismatch!");
  repeat (4) @(posedge lite_dv.clk_i);
  $finish;
end

initial begin   // slave（用同一个接口的另一个 driver 句柄被动响应）
  automatic logic [31:0] w_data, rdata;
  automatic logic [31:0] addr;
  automatic axi_pkg::prot_t prot;
  s_drv.reset_slave();
  forever begin
    // 收 AW+W，存数据，回 B
    s_drv.recv_aw(addr, prot);
    s_drv.recv_w(w_data, /*strb(忽略)*/ strb_dummy);  // 注：实际需声明 strb_dummy
    back_store <= w_data;
    s_drv.send_b(axi_pkg::RESP_OKAY);
    // 收 AR，回 R（把存的数据送回）
    s_drv.recv_ar(addr, prot);
    s_drv.send_r(back_store, axi_pkg::RESP_OKAY);
  end
end
```

**操作步骤与观察点**：

1. 把上述片段整理进一个完整 `module tb_lite_loopback;`（补上 `clk` 时钟生成与 `lite_dv` 声明，参考 [tb_axi_lite_to_axi.sv:31-34](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L31-L34) 与 [L71-L83](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L71-L83) 的时钟生成）。
2. 编译并仿真。
3. **观察/预期**：master 打印 `read back: 0000cafe`，断言通过、无 `$error`。
4. **标注 TA/TT 的作用**：把 master 的 `TA` 改成 `0ns`、`TT` 改成 `1ns`，重跑——功能仍应通过（TA/TT 只影响波形相对沿的位置，不改变握手逻辑正确性）；但波形里 `aw_valid` 会紧贴时钟沿。这正说明 **TA/TT 是「时序保真」参数，不影响功能，只影响激励相对时钟的施加/采样时刻**。
5. 若仿真器/环境不具备，本任务可作为**源码阅读型实践**：对照 [axi_lite_rand_master.write](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1698-L1709) 与 [.read](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1712-L1720)，说明示例代码里的写/读过程与库内高层任务逐行对应。

> 注：示例代码中 slave 的 `recv_w` 第二个输出参数（strb）需要一个实际变量承接（注释里的 `strb_dummy` 仅为示意）；写真实 TB 时请正常声明。具体运行结果待本地验证。

## 6. 本讲小结

- `axi_lite_driver` / `axi_driver` 是 `axi_test` 包里的**底层逐拍驱动器**，把「在一个 AXI 通道上完成一次合法握手」封装成 `send_aw`/`recv_b` 等任务，调用者不再手写握手与清零。
- driver 通过 **virtual interface** 与测试台的 `AXI_LITE_DV`/`AXI_BUS_DV` 实例绑定；`new()` 只存句柄，`reset_master()`/`reset_slave()` 严格按 modport 方向清零本侧输出。
- **TA = application time**（沿后延迟施加激励，用 `<= #TA`）、**TT = test time**（沿后延迟采样输入，用 `#TT`），二者共同模拟 clock-to-Q 与 setup/hold，使仿真时序逼近真实硬件；约定 \( 0 \le TA < TT < T_{\text{clk}} \)。
- `send_*` 驱动载荷与 `valid` 并逐拍等 `ready`（满足「valid 不撤」铁律），`recv_*` 驱动 `ready` 并等 `valid` 后采样载荷——两者完全对称。
- `axi_driver` 用 **beat 对象**（`axi_ax_beat` 等）承载完整 AXI4 的十几根信号，`recv_*`/`mon_*` 内部 `beat = new;` 防止别名；`mon_*` 不驱动任何信号、只旁观 `valid && ready`，用于监视/scoreboard。
- 最小调用范式：`reset_master()` → `@(posedge clk)` → `send_aw/send_w` → `recv_b`（写），或 `send_ar` → `recv_r`（读），范本见 [test/tb_axi_lite_to_axi.sv:85-108](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_axi.sv#L85-L108)。

## 7. 下一步学习建议

- **u3-l2 随机主从、scoreboard 与 sim_mem**：`axi_rand_master`/`axi_rand_slave` 内部正是持有一个 `axi_driver drv`，把本讲的 `send_*/recv_*` 包成自动化的随机激励；`axi_scoreboard` 则用 `mon_*` 旁路监视并自检。理解了本讲，u3-l2 会非常顺。
- **u3-l3 编写并运行一个测试台**：把本讲的 driver 套进一个完整 `tb_*.sv` 骨架（时钟、复位、接口声明、宏互连、`end_of_sim`），走通 `make sim-<tb>.log` 全流程。
- **延伸阅读**：随手翻 [src/axi_test.sv:1321-1551](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1321-L1551) 的 `axi_rand_slave`，看它如何用 `recv_aw`+`recv_w`+`send_b` 组合出一个能服务多拍突发的随机从端——这是本讲 send/recv 任务的进阶应用。
