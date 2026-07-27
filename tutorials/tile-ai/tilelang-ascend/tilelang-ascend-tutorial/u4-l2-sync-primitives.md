# 同步原语（核内流水与核间）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Ascend AI Core 内部为什么需要同步——它的多条硬件流水线（MTE2/MTE1/M/Fix/V/…）是**并行**执行的，必须显式约定先后顺序。
- 掌握核内流水同步 `T.set_flag` / `T.wait_flag` 的 **producer / dst / eventId** 语义，能读懂“预置 set + 循环内配对 + 末尾 clear”的双缓冲同步套路。
- 区分两类屏障 `T.barrier_all`（全流水线屏障）与 `T.pipe_barrier`（单流水线屏障）的代价与适用场景。
- 理解核间同步 `T.set_cross_flag` / `T.wait_cross_flag`，知道它用在 Cube 核与 Vector 核之间通过 GM 交换数据的场合。
- 看懂 `get_kernel_source()` 生成的 Ascend C / PTO 代码里这些原语分别落到哪条指令。

本讲承接 [u4-l1](u4-l1-expert-scope.md)：上一讲你学会了用 `T.Scope("C"/"V")` 与 `alloc_L1/L0C/ub` 把数据显式放到不同存储、划到不同执行域；本讲回答“这些域里的指令如何排队、跨域跨核时如何不乱序”。

## 2. 前置知识

### 2.1 Ascend AI Core 是“多流水线并行”的处理器

与 GPU 的 SM 不同，Ascend 一个 AI Core 内部不是一条流水线，而是**多条互相独立的硬件流水线（pipe / queue）**，它们可以同时推进。你必须认识的几条（大小写不敏感）：

| 流水线 | 全称 | 职责 |
|--------|------|------|
| `MTE2` | Memory Transfer Engine 2 | GM → L1 / UB 的搬运（片外 DDR 进片上） |
| `MTE1` | Memory Transfer Engine 1 | L1 → L0A / L0B 的搬运 |
| `M` | Matrix（Cube） | 矩阵乘 `Mmad`，写 L0C |
| `Fix` | fixpipe | L0C → UB 或 L0C → GM 的搬出 |
| `V` | Vector | UB 上的逐元素 / reduce 向量计算 |
| `MTE3` | Memory Transfer Engine 3 | UB → GM 的搬出 |
| `S` | Scalar | 标量计算 |

它们之间存在天然的**生产者—消费者**依赖。例如一次 GEMM：

```
MTE2 把 A、B 从 GM 搬到 L1   ──┐
                              ├── MTE1 把 L1 搬到 L0A/L0B ── M 做 mma 写 L0C ── Fix 把 L0C 搬出
```

如果任其并行，`M` 可能在 `MTE1` 还没把数据搬进 L0A 时就开算，得到错误结果。因此 Ascend 提供**事件标志（HardEvent）**机制，让你显式声明“谁做完，谁才能开始”。本讲的 6 个原语就是这套机制的前端封装。

### 2.2 两种同步粒度

- **核内（intra-core）**：同一个核内，A 流水线通知 B 流水线。用 `set_flag/wait_flag`、`barrier_all/pipe_barrier`。
- **核间（inter-core）**：Cube 核算完、Vector 核才能消费；两者经 GM/L2 交换数据。用 `set_cross_flag/wait_cross_flag`。

记住这个区分，后面四节就分别在这两个粒度上展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/language/ascend.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py) | 6 个同步原语的前端 Python 定义，全部是发射 TIR intrinsic 的薄封装 |
| [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) | README 指向的高性能 GEMM，用 `set_flag/wait_flag` 手写 MTE2→MTE1→M→Fix 多级流水 |
| [examples/simple_fusion/matmul_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/simple_fusion/matmul_add.py) | 最简 Cube+Vector 融合示例，用 `set_cross_flag/wait_cross_flag` 做核间同步 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | Ascend C 后端：把这些 intrinsic 翻译成 `AscendC::SetFlag/WaitFlag/PipeBarrier/CrossCore*` |
| [src/target/codegen_ascend_pto.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc) | PTO 后端：翻译成 `set_flag_pipeline/wait_flag_pipeline/pipe_barrier/set_cross_flag` 等 PTO IR |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 第 4.1.4 节“同步原语”官方参数说明 |

---

## 4. 核心概念与源码讲解

### 4.1 同步的心智模型：producer / dst / eventId

#### 4.1.1 概念说明

在看 6 个原语前，先建立统一模型。Ascend 的事件同步本质是**“信号量式”**的：

- **set（置位）**：某条流水线干完一段活，挂出一个“事件完成”的牌子。
- **wait（等待）**：另一条流水线在开始下一段活前，先看牌子在不在；不在就阻塞。

一个牌子由三元组唯一确定：

- **producer pipe（src）**：谁挂的牌子（谁完成的）。
- **consumer pipe（dst）**：牌子是给谁看的（谁在等）。
- **eventId**：牌子编号。同一对 `(src, dst)` 之间可以同时挂多个牌子，用编号区分——这正是**双缓冲/多缓冲**的关键：第 0 趟数据用 eventId 0，第 1 趟用 eventId 1，循环复用。

> 命名提示：本项目中 `set_flag(src, dst, eventId)` 的 `src` 就是 **producer**（置位方），`dst` 是 **consumer**（等待方）。配对的 `set_flag` 与 `wait_flag` 的 `(src, dst, eventId)` 必须完全一致。

合法的 pipe 字符串在源码里用一个 `Literal` 写死：

```python
_pipe = Literal["fix", "mte1", "mte2", "mte3", "m", "v", "s"]
```

[tilelang/language/ascend.py:8](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L8) — 这就是 7 条流水线的枚举。

#### 4.1.2 核心流程

一次典型的“搬运→计算”同步：

```
MTE2: copy GM→L1  ──► set_flag("mte2","mte1",0)      # 搬完，挂牌子 0
                                  │
MTE1: wait_flag("mte2","mte1",0) ◄┘  copy L1→L0      # 先看牌子 0，再搬
      set_flag("mte1","m",0)                          # 搬完，给 M 挂牌子
M:    wait_flag("mte1","m",0)  mma                    # 等 MTE1，再算
```

规则就两条：

1. **配对**：每个 `wait` 必须有且仅有一个 `src/dst/eventId` 相同的 `set` 与之对应。
2. **平衡**：一个核程序结束时，所有 eventId 的 set/wait 次数必须配平，否则硬件事件队列会溢出/死锁。这正是后面“末尾 clear”套路的由来。

### 4.2 核内流水同步：T.set_flag / T.wait_flag

#### 4.2.1 概念说明

`T.set_flag` / `T.wait_flag` 是最基础、最常用的核内同步原语，专门表达“同一条核内、A 流水线 → B 流水线”的依赖。它们是 Expert 模式下手动搭软件流水（software pipeline）的核心积木；Developer 模式里它们通常由自动同步 pass 替你插（见 [u4-l3](u4-l3-auto-sync.md)），但理解手写版本是看懂高性能 kernel 的前提。

#### 4.2.2 核心流程：双缓冲的“预置—循环—清零”三段式

直接给每个搬运配一对 set/wait 只能保证正确，无法重叠。要真正让 MTE2 与 M 重叠（一边搬下一块、一边算当前块），需要**多缓冲 + 事件编号循环复用**。`example_gemm_intrinsic.py` 给出了标准写法，分三段：

```
段一 init_flag：进入循环前，预先 set 一批 flag
        ── 伪造“上一轮已完成”，让第一轮的 wait 不至于死锁
段二 循环体：每个缓冲槽 k%S 用 eventId = k%S 复用
        wait(数据就绪) → 搬/算 → set(本槽完成 / 槽已释放)
段三 clear_flag：循环结束后，把段一预置的 flag wait 掉
        ── 配平 set/wait 计数，保持事件队列干净
```

#### 4.2.3 源码精读

**前端定义**——两个函数都只是把字符串参数 `.upper()` 后塞进 intrinsic 调用：

```python
def set_flag(src: _pipe, dst: _pipe, eventId: int):
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_set_flag"), src.upper(), dst.upper(), eventId)

def wait_flag(src: _pipe, dst: _pipe, eventId: int):
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_wait_flag"), src.upper(), dst.upper(), eventId)
```

[tilelang/language/ascend.py:158-173(Lset_flag)](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L158-L173)、[tilelang/language/ascend.py:176-197(Lwait_flag)](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L176-L197)。

注意 `src.upper()`：所以前端写 `"mte2"` 或 `"MTE2"` 都行，最终统一成大写交给 codegen。

**双缓冲同步套路**（高性能 GEMM，省略无关行）：

```python
@T.macro
def init_flag():                      # 段一：预置 flag
    T.set_flag("mte1", "mte2", 0)     # 假装 L1 槽 0/1 已被 MTE1 释放，MTE2 可写入
    T.set_flag("mte1", "mte2", 1)
    T.set_flag("m", "mte1", 0)        # 假装 L0 槽 0/1 数据已被 M 消费，MTE1 可覆盖
    T.set_flag("m", "mte1", 1)
    T.set_flag("fix", "m", 0)         # 假装 L0C 已被 Fix 搬走，M 可覆盖

@T.macro
def clear_flag():                     # 段三：清掉段一预置的 flag，配平计数
    T.wait_flag("mte1", "mte2", 0)
    T.wait_flag("mte1", "mte2", 1)
    T.wait_flag("m", "mte1", 0)
    T.wait_flag("m", "mte1", 1)
    T.wait_flag("fix", "m", 0)
```

[examples/gemm/example_gemm_intrinsic.py:28-42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L28-L42)。

循环体里，搬 L1→L0 前 `wait_flag("mte2","mte1",k%S1)` 等 GM→L1 完成；搬完 `set_flag("mte1","m",kk%S2)` 通知 M；M 算完 `set_flag("m","mte1",kk%S2)` 回报“L0 槽可复用”：

```python
T.wait_flag("mte2", "mte1", k % S1)            # 等 L1 槽 k 数据就绪
T.copy(A_L1[k % S1, 0, kk*block_K], A_L0[kk % S2, :, :])   # MTE1: L1→L0
...
T.set_flag("mte1", "m", kk % S2)               # 通知 M: L0 数据就绪
T.wait_flag("mte1", "m", kk % S2)              # M 等
T.mma(A_L0[...], B_L0[...], C_L0, init=...)    # M: mma
T.set_flag("m", "mte1", kk % S2)               # M 回报: L0 槽可复用（下一轮 MTE1 可覆盖）
```

[examples/gemm/example_gemm_intrinsic.py:85-96](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L85-L96)。

> 配对练习：`set_flag("mte2","mte1",k%S1)` 的配对 `wait` 是上面这条 `wait_flag("mte2","mte1",k%S1)`；而 `set_flag("m","mte1",kk%S2)` 配的 `wait` 在下一轮迭代里出现——这正是“M 释放 L0 槽 → 下一轮 MTE1 才能覆盖”的跨迭代依赖。

**codegen（Ascend C 后端）** 把 intrinsic 翻译成 `AscendC::SetFlag<HardEvent::SRC_DST>(eventId)`：

```cpp
void CodeGenTileLangAscend::FlagOpCodegen(const CallNode *op, std::string op_name) {
  std::string src = Downcast<StringImm>(op->args[0])->value;
  std::string dst = Downcast<StringImm>(op->args[1])->value;
  op_name += "<AscendC::HardEvent::" + src + "_" + dst + ">";
  PrintOpCall(op, op_name, {0, 0}, {2, op->args.size()});
}
```

[src/target/codegen_ascend.cc:2468-2475](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2468-L2475)。所以 `set_flag("mte2","mte1",0)` 会生成 `AscendC::SetFlag<AscendC::HardEvent::MTE2_MTE1>(0);`，`wait_flag(...)` 生成 `AscendC::WaitFlag<AscendC::HardEvent::MTE2_MTE1>(0);`。分发入口在 [src/target/codegen_ascend.cc:652-655](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L652-L655)。

**codegen（PTO 后端）** 走另一套名字——`set_flag_pipeline<PIPE_MTE2, PIPE_MTE1>(eventId)`：

```cpp
this->stream << kAscendPtoScope << op_name << "_pipeline<PIPE_" << src << ", "
             << "PIPE_" << dst << "> (" << event_id << ");\n";
```

[src/target/codegen_ascend_pto.cc:1896-1904](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1896-L1904)。两条后端路线语义一致、指令名不同——这就是为什么同一份 kernel 可以在 `ascendc` 与 `pto` 两个 target 下都跑通。

#### 4.2.4 代码实践

**实践目标**：用 `get_kernel_source()` 看清 `set_flag/wait_flag` 最终生成了什么，并验证“去掉一个 set 会导致死锁/结果错误”的因果。

**操作步骤**：

1. 打开 `examples/gemm/example_gemm_intrinsic.py`，在文件末尾的 `print(func.get_kernel_source())`（[第 110 行](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L110)）后运行脚本。
2. 在打印出的 C++ 源码里搜索 `SetFlag` 与 `WaitFlag`，找到形如 `AscendC::SetFlag<AscendC::HardEvent::MTE2_MTE1>(0);` 的行。
3. 对照前端 `set_flag("mte2","mte1",0)`，确认 src/dst/eventId 一一对应。
4. **破坏性实验**：把 `clear_flag()` 宏里任意一行 `T.wait_flag(...)` 注释掉，重新编译运行。

**需要观察的现象**：

- 正常情况下末尾打印 `Kernel Output Match!`。
- 注释掉一个 `wait_flag` 后，事件队列计数失衡，很可能出现硬件报错或结果错误（具体表现**待本地验证**，取决于驱动版本）。

**预期结果**：你能把前端每一句 `set/wait` 在生成代码里找到对应行，证明这条链路是“前端 intrinsic → codegen → AscendC 指令”的直译。

> 注：本实践需要真实的昇腾 NPU 与 CANN 环境。若无硬件，可只做第 1–3 步的源码对照（`get_kernel_source` 即使在仿真模式也能生成源码文本）。

#### 4.2.5 小练习与答案

**练习 1**：`example_gemm_intrinsic.py` 里 `S1=2`、`S2=2`（见 [第 108 行](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L108) 传参）。如果把 `S1` 改成 3，需要同步修改哪些地方？

**答案**：`init_flag` 与 `clear_flag` 里的 `mte1↔mte2` flag 需要扩到 eventId `0/1/2`（再加一组 `set_flag("mte1","mte2",2)` 与对应的 `wait`），否则只有 2 个槽被预置、第 3 个槽的首次 `wait` 会死锁。`S2` 同理影响 `m↔mte1` 的 flag 数量。

**练习 2**：为什么 `set_flag("m","mte1",kk%S2)` 和它的配对 `wait` 不在同一个循环迭代里？

**答案**：`set_flag("m","mte1",...)` 表示“M 已消费完某个 L0 槽，可被下一轮 MTE1 覆盖”。它的配对 `wait` 出现在**下一轮** MTE1 要写该槽之前——这是跨迭代的缓冲复用依赖，正是双缓冲能重叠搬运与计算的根源。

---

### 4.3 屏障类同步：T.barrier_all / T.pipe_barrier

#### 4.3.1 概念说明

逐对 `set/wait` 表达力强但繁琐。当你只想要“一段计算彻底做完，再开始下一段”时，屏障（barrier）更顺手。tile-lang 提供两个粒度：

- `T.barrier_all()`：**全流水线屏障**——在该点之前**所有**流水线发出的指令都必须完成，之后任何流水线的指令才能开始。最“重”，但最安全、最直观。
- `T.pipe_barrier(pipe)`：**单流水线屏障**——只保证指定那条流水线（如 `"V"`）内部前一段指令完成。更轻，常用于保证某个向量指令（如 `brcb`）的结果可被后续指令读到。

在 [u1-l4](u1-l4-first-gemm.md) 的入门 GEMM 里你已经见过 `T.barrier_all()` 的用法：它被当作“一算一歇”的简易同步，省去了手写 flag 的负担，代价是牺牲了流水线重叠。

#### 4.3.2 核心流程

```
barrier_all  ： ──[MTE2][MTE1][M][Fix][V] 全部 drain──► 才放行后续
pipe_barrier(V)： ──[V 内部指令] drain ──► 才放行 V 的后续（不影响其他 pipe）
```

经验法则：

- **开发/调试阶段**优先用 `barrier_all`，简单不易错。
- **追求性能时**用细粒度 `set/wait_flag` 替换 `barrier_all`，把可重叠的搬运/计算解放出来。
- 需要保证同一条流水线内顺序（例如先广播 `brcb`、再读广播结果）时，用 `pipe_barrier("V")`。

#### 4.3.3 源码精读

**前端**——`barrier_all` 其实就是 `pipe_barrier` 传了特殊值 `"ALL"`：

```python
def barrier_all():
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_pipe_barrier"), "ALL")

def pipe_barrier(pipe: _pipe):
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_pipe_barrier"), pipe.upper())
```

[tilelang/language/ascend.py:200-210(Lbarrier_all)](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L200-L210)、[tilelang/language/ascend.py:213-226(Lpipe_barrier)](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L213-L226)。两者共用同一个 intrinsic `tl.ascend_pipe_barrier`，仅参数不同。

**简单用法**（`matmul_add.py` 里 Cube 段的 K 循环，用 `barrier_all` 串起搬入—gemm—搬出）：

```python
with T.Scope("C"):
    for k in T.serial(loop_k):
        T.copy(A[bx*block_M, k*block_K], A_L1)      # MTE2
        T.copy(B[k*block_K, by*block_N], B_L1)      # MTE2
        T.barrier_all()                              # 等 MTE2 全搬完
        ...T.gemm_v0(A_L1, B_L1, C_L0, ...)          # M
        T.barrier_all()                              # 等 M 算完
    T.copy(C_L0, C[bx*block_M, by*block_N])          # Fix
```

[examples/simple_fusion/matmul_add.py:48-60](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/simple_fusion/matmul_add.py#L48-L60)。对比 4.2 节的 flag 写法，这里的同步是“一刀切”，正确但慢。

**codegen（Ascend C 后端）** 生成 `AscendC::PipeBarrier<PIPE_...>`：

```cpp
void CodeGenTileLangAscend::PipeBarrierCodegen(const CallNode *op) {
  std::string pipe = Downcast<StringImm>(op->args[0])->value;
  std::string op_name = "AscendC::PipeBarrier<PIPE_" + pipe + ">";
  PrintOpCall(op, op_name, {0, 0}, {0, 0});
}
```

[src/target/codegen_ascend.cc:2477-2483](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2477-L2483)。于是 `barrier_all()` → `AscendC::PipeBarrier<PIPE_ALL>();`，`pipe_barrier("V")` → `AscendC::PipeBarrier<PIPE_V>();`。

**codegen（PTO 后端）** 生成 `pipe_barrier(PIPE_...)`，并有一个 A5 特例——A5 平台上 `PIPE_V` 的 barrier 会被跳过（硬件保证）：

```cpp
std::string pipe = Downcast<StringImm>(op->args[0])->value;
if (this->platform_ == "A5" && pipe == "V") { return; }   // A5: V 的 barrier 由硬件隐式保证
this->stream << "pipe_barrier(PIPE_" << pipe << ");\n";
```

[src/target/codegen_ascend_pto.cc:1886-1894](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1886-L1894)。这是“同一前端、不同硬件后端有不同优化”的一个典型例子。

#### 4.3.4 代码实践

**实践目标**：直观感受 `barrier_all` 与细粒度 flag 的性能差距。

**操作步骤**：

1. 准备两个版本的 GEMM：A 版直接用 `examples/gemm/example_gemm.py`（入门版，K 循环内用 `T.barrier_all()`，见 [u1-l4](u1-l4-first-gemm.md)）；B 版用 `examples/gemm/example_gemm_intrinsic.py`（flag 版）。
2. 用相同 M/N/K（如默认 8192/1024/8192）分别跑 `do_bench`。
3. 比较两者打印的 `tilelang time`。

**需要观察的现象**：B 版（flag 重叠流水）应明显快于 A 版（barrier 一刀切）。

**预期结果**：flag 版时延更低（具体倍数**待本地验证**，取决于硬件与 shape）。

#### 4.3.5 小练习与答案

**练习 1**：`barrier_all()` 和 4.2 节的全套 `set/wait_flag` 在表达力上是什么关系？

**答案**：`barrier_all()` 等价于“对所有相关 `(src,dst)` 对各插一组 set/wait”，是一种过近似（over-approximation）——它禁止了所有并行，而其中很多 `(src,dst)` 对其实没有真实依赖。`set/wait_flag` 只约束真正有数据依赖的对，因而能保留更多可重叠的并行。

**练习 2**：为什么 PTO 后端在 A5 上会跳过 `pipe_barrier("V")`？

**答案**：A5 的 Vector 流水线内部由硬件保证按序完成（in-order），软件再插一道 V 屏障是冗余的，跳过可减少指令数。这正是后端按 `platform_` 做差异化优化的体现。

---

### 4.4 核间同步：T.set_cross_flag / T.wait_cross_flag

#### 4.4.1 概念说明

到这里为止，所有同步都发生在**一个核内部**。但 Ascend 的 Cube 核（管 L1/L0/mma）与 Vector 核（管 UB/向量）经常分工合作：Cube 算出 L0C、写回 GM，Vector 再从 GM 读进来做后处理（softmax、加偏置……）。两者是**不同的核**，4.2 的核内 flag 对它们无效——你需要**核间**事件：`set_cross_flag` / `wait_cross_flag`。

这对原语是 [u5-l1](u5-l1-combine-cv.md) CV 分离、[u5-l2](u5-l2-cross-core-pipeline.md) 跨核流水、[u5-l4](u5-l4-workspace-reduction.md) workspace 消除的手写版基础；Developer 模式下它们可由自动 CV 同步 pass 代劳。

#### 4.4.2 核心流程

典型 Cube→Vector 协作：

```
Cube 核： ...Fix 把结果写 GM...  ──► set_cross_flag("FIX", 0)     # 通知：GM 数据可读
                                                  │
Vector 核：wait_cross_flag(0) ◄────────────────────┘  copy GM→UB   # 等 Cube，再读
```

注意与核内 flag 的两个关键区别：

1. **wait 不带 src/dst**：`wait_cross_flag(flag)` 只需 eventId。因为核间信号是“广播式”的，等待方只关心“编号为 flag 的事件到了没”，不需要指明来自哪条 pipe。
2. **set 多一个 `mode` 参数**：约定这套信号在什么范围内生效（见下文源码）。

#### 4.4.3 源码精读

**前端定义**：

```python
def set_cross_flag(pipe: str, flag: int, mode: int = 2):
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_set_cross_flag"), pipe.upper(), flag, mode)

def wait_cross_flag(flag: int, pipe: _pipe | Literal[""] = ""):
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_wait_cross_flag"), flag, pipe)
```

[tilelang/language/ascend.py:116-135(Lset_cross_flag)](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L116-L135)、[tilelang/language/ascend.py:138-155(Lwait_cross_flag)](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L138-L155)。

`mode` 的三种含义（来自源码注释）：

- `0`：在所有 AIC 之间，或在所有 AIV 之间同步。
- `1`：在同一 group 内的所有 AIV 之间同步。
- `2`（默认）：在**同一 group 内的 AIC 与 AIV 之间**同步——这是 Cube↔Vector 最常用的模式。

> 平台限制：`wait_cross_flag` 的 `pipe` 参数**仅 A5 平台支持**，其他架构必须留空字符串。源码注释明确写了这一点（[tilelang/language/ascend.py:147-149](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L147-L149)）。

**最简核间同步示例**（`matmul_add.py`，Cube 算 C=A@B，Vector 做 C=D+C）：

```python
with T.Scope("C"):
    ...T.copy(C_L0, C[bx*block_M, by*block_N])   # Fix: 把 Cube 结果写回 GM
    T.set_cross_flag("FIX", 0)                   # Cube 通知 Vector: GM 可读

with T.Scope("V"):
    T.wait_cross_flag(0)                         # Vector 等 Cube 通知
    T.copy(C[bx*block_M + vid*..., by*block_N], c_ub)   # 再从 GM 读
    ...
```

[examples/simple_fusion/matmul_add.py:60-68](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/simple_fusion/matmul_add.py#L60-L68)。这一对 `set_cross_flag("FIX",0)` / `wait_cross_flag(0)` 就建立了 Cube→Vector 的依赖：没有它，Vector 可能在 Cube 还没写完 GM 时就去读，读到脏数据。

**codegen（Ascend C 后端）** 生成 `AscendC::CrossCoreSetFlag<mode, PIPE_...>(flag)` 与 `AscendC::CrossCoreWaitFlag(flag)`：

```cpp
void CodeGenTileLangAscend::SetCrossFlagCodegen(const CallNode *op) {
  std::string pipe = Downcast<StringImm>(op->args[0])->value;
  int mode = op->args[2].as<IntImmNode>()->value;
  std::string op_name = "AscendC::CrossCoreSetFlag<0x";
  op_name.append(std::to_string(mode));
  op_name.append(", PIPE_");  op_name.append(pipe);  op_name.append(">");
  PrintOpCall(op, op_name, {0, 0}, {1, op->args.size() - 1});
}
```

[src/target/codegen_ascend.cc:2456-2466](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2456-L2466)。`wait_cross_flag` 的分发在 [src/target/codegen_ascend.cc:648-649](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L648-L649)，生成 `AscendC::CrossCoreWaitFlag(flag);`。

所以 `set_cross_flag("FIX", 0, 2)` → `AscendC::CrossCoreSetFlag<0x2, PIPE_FIX>(0);`，`wait_cross_flag(0)` → `AscendC::CrossCoreWaitFlag(0);`。

**codegen（PTO 后端）** 生成 `pto::set_cross_flag<PIPE_FIX>(flag, mode)` / `pto::wait_cross_flag(flag)`，并在 `current_resource_scope_` 未知时打印警告（[src/target/codegen_ascend_pto.cc:1929-1999](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1929-L1999)）。

#### 4.4.4 代码实践

**实践目标**：跑通 Cube+Vector 融合算子，并验证“删掉 cross_flag 会导致结果错误”，从而确认核间依赖的必要性。

**操作步骤**：

1. 运行 `examples/simple_fusion/matmul_add.py`，确认末尾打印 `Kernel Output Match!`（[第 93 行](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/simple_fusion/matmul_add.py#L93)）。
2. 在生成源码里（`print` 或在 [u1-l5](u1-l5-jit-and-pipeline.md) 学到的 `func.get_kernel_source()`）搜索 `CrossCoreSetFlag` / `CrossCoreWaitFlag`，定位 Cube 段的 set 与 Vector 段的 wait。
3. **破坏性实验**：注释掉 Cube 段的 `T.set_cross_flag("FIX", 0)`（[第 62 行](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/simple_fusion/matmul_add.py#L62)），重新运行。

**需要观察的现象**：

- 正常：`Kernel Output Match!`。
- 删除后：Vector 可能在 Cube 写 GM 完成前读 C，`torch.testing.assert_close` 大概率失败（具体是否必现**待本地验证**，存在时序竞争）。

**预期结果**：核间 cross_flag 是保证 Cube→Vector 数据可见性的必要条件；删除后结果不稳定或错误。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `wait_cross_flag` 不像 `wait_flag` 那样需要 `(src, dst)` 两个 pipe 参数？

**答案**：核内 `wait_flag(src,dst,id)` 需要指明“等哪条 pipe 产生的、给哪条 pipe 看的”事件，因为核内有多条 pipe 两两之间都可能要同步。核间信号是面向“另一个核”的广播事件，等待方只需 eventId 即可——它等的是“编号为 id 的跨核事件被置位”，与具体哪条 pipe 置位无关（置位方的 pipe 信息已在对应的 `set_cross_flag` 里编码进硬件）。

**练习 2**：`set_cross_flag` 的 `mode` 默认是 2。如果两个需要同步的计算都在 Vector 核（AIV）上、且属于同一 group，应该用哪个 mode？

**答案**：用 `mode=1`（同一 group 内所有 AIV 之间同步）。`mode=2` 是 AIC↔AIV（Cube↔Vector）专用，AIV↔AIV 应用 1，跨所有核的 AIV↔AIV 用 0。

---

## 5. 综合实践

把本讲的核内、核间同步串起来，做一个“读懂高性能 GEMM 同步全貌”的源码阅读任务：

1. 打开 [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py)。
2. 列一张表，把程序里**每一个** `set_flag` 与其配对的 `wait_flag` 用 `(src, dst, eventId)` 连起来（注意有些配对跨迭代、有些在 `init_flag/clear_flag` 宏里）。
3. 画一张时序图：横轴是循环迭代 `k`/`kk`，纵轴是 MTE2/MTE1/M/Fix 四条 pipe，把 `copy`、`mma`、`set/wait` 标到对应位置，用箭头表示 wait→set 的依赖。重点标出“MTE2 搬第 k+1 块”与“M 算第 k 块”在时间上是如何**重叠**的——这就是软件流水隐藏延迟的本质。
4. 回答：这套同步里，`mte1→mte2` 方向的 flag 承担什么职责？（提示：与 L1 缓冲槽的复用有关。）
5. 进阶（可选）：参考 [examples/simple_fusion/matmul_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/simple_fusion/matmul_add.py)，给上面的 GEMM 再接一个 Vector 后处理（如对 C 做 `relu`），用 `set_cross_flag/wait_cross_flag` 把 Cube 写 GM 与 Vector 读 GM 串起来。

> 这个任务不依赖运行硬件（纯源码阅读 + 画图），但若能在 NPU 上跑通第 5 步并看到 `Kernel Output Match!`，则完成端到端验证。

## 6. 本讲小结

- Ascend AI Core 内部有 MTE2/MTE1/M/Fix/V/MTE3/S 等多条**并行流水线**，必须用事件机制显式约束先后——这是本讲所有同步原语存在的根本原因。
- `T.set_flag(src,dst,eventId)` / `T.wait_flag(...)` 表达**核内** producer→consumer 依赖；`eventId` 用于多缓冲复用，配对的 set/wait 参数必须完全一致且最终计数配平。
- 高性能 kernel 的标准套路是“预置 `init_flag` → 循环内 `k%S` 复用缓冲与 eventId → 末尾 `clear_flag` 配平”，见 `example_gemm_intrinsic.py`。
- `T.barrier_all()` 是全流水线屏障（最安全但最慢），`T.pipe_barrier(pipe)` 是单流水线屏障；开发期用前者，调优期用细粒度 flag 替换。
- `T.set_cross_flag(pipe,flag,mode)` / `T.wait_cross_flag(flag)` 表达**核间**（典型 Cube↔Vector）依赖，是 CV 协作的基础；`mode=2` 是同组 AIC↔AIV 默认模式，`wait_cross_flag` 的 `pipe` 参数仅 A5 支持。
- 这些前端 intrinsic 经 `codegen_ascend.cc`（Ascend C）与 `codegen_ascend_pto.cc`（PTO）分别落到 `AscendC::SetFlag/...` 与 `set_flag_pipeline/...` 两套指令，语义一致、指令名不同。

## 7. 下一步学习建议

- 手写 flag 繁琐易错，下一讲 [u4-l3 自动同步插入](u4-l3-auto-sync.md) 讲 `AscendSyncInsert` pass 与 `TL_ASCEND_AUTO_SYNC` 开关，让编译器在 Developer 模式下替你插这些 flag。
- 本讲的 cross_flag 只是单点核间同步；当你想让 Cube 与 Vector **持续重叠**（一边算一边交换数据）时，进入 [u5 单元](u5-l1-combine-cv.md)：CV 分离、[跨核流水](u5-l2-cross-core-pipeline.md)、workspace 消除都建立在 cross_flag 之上。
- 想看本讲原语在真实大算子里的复杂用法，可直接读 [examples/gqa_fwd_varlen/gqa_fwd_varlen.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gqa_fwd_varlen/gqa_fwd_varlen.py)，里面用多个命名的 `SEM_WS*_C2V/V2C` 事件号管理多段 workspace 的核间流水。
