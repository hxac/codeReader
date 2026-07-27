# 调试与性能分析

## 1. 本讲目标

本讲解决两个最常困扰昇腾算子开发者的工程问题：「**算出来的数对不对**」和「**算得够不够快**」。读完本讲，你应当能够：

- 用 `func.get_kernel_source()` 在**不跑硬件**的情况下审视 tile-lang 为你生成的 Ascend C 代码，快速定位「为什么编不过 / 为什么生成得不对」。
- 在 kernel 内部插入 `T.printf` 与 `T.dump_tensor`，把设备端（AI Core 内部）任意一块 buffer 的值打印到 host，验证搬运、累加、归约等中间结果。
- 理解 `TL_PTO_DEBUG` 这个总开关的**触发条件、性能代价、后端差异与 1 MB 空间上限**，知道何时开、何时必须关。
- 用 CANN 提供的 `msprof op`（上板）与 `msprof op simulator`（仿真）采集算子性能数据，定位算子是「算力 bound」还是「带宽 bound」。

本讲是整个实战单元（u7）的「工具箱」：后面的 FlashAttention（u7-l1）、高性能 GEMM（u7-l2）、贡献新算子（u7-l7）都依赖这里介绍的调试与采集手段。

## 2. 前置知识

在进入本讲前，你需要先具备以下认知（来自前置讲义）：

- **JIT 与编译链路（u1-l5）**：`@tilelang.jit` 装饰的函数首次调用时，会经 `lower()`（LowerAndLegalize → OptimizeForTarget → device_codegen）产出一份 **Ascend C 源码**，再由 CANN 的 **bisheng 编译器**编成 `.so`，最后 ctypes 加载执行。本讲的调试手段，本质上都是在「观察 / 干预」这条链路的产物。
- **双 Codegen（u6-l2）**：tile-lang 在昇腾上有两条 codegen 路线——`ascendc`（对象模型，落到 `AscendC::` API）与 `pto`（指令宏模型，落到 `TASSIGN/TMOV/...`）。**同一个 intrinsic 在两条路线生成风格不同的 C++**，这点对理解打印输出至关重要。
- **片上存储层级（u3-l1）**：GM / L1 / UB / L0A·L0B·L0C。`T.dump_tensor` 能转储的正是这几类 buffer。

几个本讲会用到的术语，先统一口径：

| 术语 | 含义 |
|------|------|
| **设备端（device-side）** | 运行在 AI Core 上的 kernel 代码（`_kernel` 函数）。`T.printf`/`T.dump_tensor` 是设备端调试工具。 |
| **主机端（host-side）** | 运行在 CPU 上的 Python / `call` 启动器。主机端直接用 Python 内置 `print` 即可，不要用 `T.printf`。 |
| **bisheng（毕昇）** | CANN 提供的 C/C++ 编译器，tile-lang 用它把生成的 Ascend C 源码编成 `.so`。ascendc 走 `-xasc`、pto 走 `-xcce`。 |
| **msprof** | CANN 提供的算子级性能采集工具，分 `msprof op`（上板）与 `msprof op simulator`（仿真）两种。 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `examples/print/elementwise_print.py` | 唯一可运行的打印示例：一个 bitwise_and kernel，演示 `T.printf` + `T.dump_tensor` 的完整用法 |
| `examples/print/README.md` / `docs/tutorials/print.md` | 打印接口的官方说明文档（两者内容几乎一致） |
| `tilelang/language/ascend.py` | `T.printf` 与 `T.dump_tensor` 的**前端定义**：把 Python 调用翻译成 TIR intrinsic |
| `tilelang/jit/adapter/libgen.py` | `resolve_compile_flags`：根据 target + `TL_PTO_DEBUG` 推导 bisheng 编译标志 |
| `tilelang/transform/pass_config.py` | `normalize_compiler_options`：读取 `TL_PTO_DEBUG` 环境变量 |
| `src/target/codegen_ascend_pto.cc` | pto 后端 codegen：把 `tl.ascend_printf`/`tl.ascend_dump_tensor` 翻成 `cce::printf` / `tl::ascend_pto::DumpTensor` |
| `src/target/codegen_ascend.cc` | ascendc 后端 codegen：翻成 `AscendC::PRINTF` / `tl::ascend::DumpTensor` |
| `src/tl_templates/pto/printf.h` | pto 模板库：`DumpTensor` 的真实实现，**受 `_DEBUG` 宏门控** |
| `src/tl_templates/ascend/printf.h` | ascendc 模板库：薄包装，落到 `AscendC::DumpTensor` |
| `tilelang/jit/kernel.py` | `JITKernel.get_kernel_source()`：返回生成的 kernel 源码 |
| `docs/TileLang-Ascend Programming Guide.md` | 第 5 节（调试诊断）与第 6 节（msProf 性能调优）的权威说明 |

## 4. 核心概念与源码讲解

本讲按「**由贱到贵**」的工具成本排列五个最小模块：先讲最便宜、零运行开销的「看源码」，再讲有运行开销的「设备端打印」，再讲打印总开关 `TL_PTO_DEBUG` 的代价与限制，最后讲性能采集 `msprof`。

### 4.1 get_kernel_source：不跑硬件就能看的「第一手证据」

#### 4.1.1 概念说明

tile-lang 的 kernel 不是直接跑 Python，而是先被**生成**成一份 Ascend C 源码，再被 bisheng 编译。这意味着：当算子「编不过」「结果不对」时，你最该看的第一份材料，不是 Python 代码，而是**生成出来的那份 C++ 代码**——它才是真正交给硬件的东西。

`func.get_kernel_source()` 就是把这份 C++ 源码以字符串形式返回给你。它是 tile-lang 里**成本最低、信息量最大**的调试手段：

- 零硬件依赖：不需要 NPU，不需要 bisheng，甚至不需要真正编译——只要 lowering 走完、codegen 出了源码就行。
- 能直接看到每个 `T.copy`/`T.gemm_v0`/`T.barrier_all` 被翻译成了什么 AscendC / PTO 调用。
- 能确认同步、布局、内存分配是否按预期插入。

#### 4.1.2 核心流程

```text
func = matmul(...)            # @tilelang.jit 工厂调用，返回 JITKernel
src = func.get_kernel_source()  # 取回 codegen 产出的 Ascend C 源码字符串
print(src)                     # 人眼审视 / grep 关键符号
```

调用链：`JITKernel.get_kernel_source()` 在 cython / ctypes 后端下委托给 `adapter.get_kernel_source()`，返回包裹后的完整源码（`wrapped_source`）；其它执行后端则直接取 `artifact.kernel_source`。

#### 4.1.3 源码精读

入口在 [tilelang/jit/kernel.py:378-389](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L378-L389)，按执行后端分流取源码：

```python
def get_kernel_source(self) -> str:
    if self.execution_backend in {"ctypes", "cython"}:
        return self.adapter.get_kernel_source()
    return self.artifact.kernel_source
```

默认 cython 后端的 adapter 实现在 [tilelang/jit/adapter/cython/adapter.py:484-490](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L484-L490)，传 `kernel_only=True` 可只取设备函数 `_kernel` 的全局源码、去掉 host 包装：

```python
def get_kernel_source(self, kernel_only: bool = False):
    if kernel_only:
        return self.kernel_global_source
    else:
        assert self.wrapped_source is not None, "Wrapped source is not available"
        return self.wrapped_source
```

> 提示：生成这份源码的源头在 `device_codegen`（u6-l2）。当你看到 `AscendC::` 前缀说明走的是 ascendc 路线，看到 `TADD`/`TMOV`/`tl::ascend_pto::` 前缀说明走的是 pto 路线——这能帮你判断接下来该查哪份模板库。

#### 4.1.4 代码实践

1. **实践目标**：学会用 `get_kernel_source()` 看 tile-lang 为 GEMM 生成的 Ascend C 代码。
2. **操作步骤**：
   - 打开 `examples/gemm/example_gemm.py`，在 `c = func(a, b)` 这一行**之前**插入：
     ```python
     print(func.get_kernel_source(kernel_only=True))
     ```
   - 重新运行 `python examples/gemm/example_gemm.py --m 128 --n 256 --k 64`。
3. **需要观察的现象**：终端先打印一大段 C++ 源码，其中能看到 `_kernel` 函数体，里面有 `T.copy` 翻译出的搬运调用、`T.gemm_v0` 翻译出的矩阵乘调用、`T.barrier_all` 翻译出的屏障调用。
4. **预期结果**：源码正常打印，随后才出现 `init successful!` 与 `Kernel Output Match!`。如果只想看设备函数、不想要 host 包装，`kernel_only=True` 会给你更干净的输出。
5. 若你的环境未装 bisheng / NPU，**只要 lowering 能走完、codegen 能产出源码**，这一步仍可打印出源码字符串——属于「源码阅读型实践」，不依赖硬件运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `get_kernel_source()` 是「成本最低」的调试手段？
**参考答案**：它只读取 codegen 已经产出的源码字符串，既不需要把源码交给 bisheng 编译，也不需要 NPU 运行，甚至不需要等 JIT 真正「编译 + 加载」完成，因此没有任何运行时开销，适合反复审视。

**练习 2**：生成的源码里同时有 `_kernel`（设备函数）和 `call`（host 启动器），想只看前者该传什么参数？
**参考答案**：`func.get_kernel_source(kernel_only=True)`，它返回 `kernel_global_source`，只含设备函数部分。

---

### 4.2 T.printf：设备端格式化打印

#### 4.2.1 概念说明

`get_kernel_source` 只能看「代码长什么样」，看不到「运行时变量的值」。当你怀疑某个下标算错、某个标志位没置对、某个 cid/vid 取值异常时，需要在 **AI Core 内部** 把值打出来——这就是 `T.printf` 的用途。

它和 C 语言的 `printf` 几乎一样：传一个格式串 + 若干参数，设备端执行到这行时把信息打印到 host。注意它是**设备端**工具，打印发生在每个核上；host 侧的 Python 调试请直接用 `print`。

#### 4.2.2 核心流程

```text
T.printf("cid=%d vid=%d\n", cid, vid)
   │
   │  前端：格式串转义 + Buffer 指针化
   ▼
tir.call_intrin("handle", Op.get("tl.ascend_printf"), escaped_fmt, *args)
   │
   │  codegen 分发（按 target）
   ├── ascendc ──▶ AscendC::PRINTF(fmt, args...)
   └── pto     ──▶ cce::printf(fmt, args...)
```

前端会做两件预处理：① 把 `%p` 统一改写成 `0x%x`（推荐用 `%x` 打印地址）；② 对格式串和字符串参数做 `unicode_escape`，让 `\n`、`\t` 这类转义在生成的 C++ 里仍合法；③ 若某个参数是 `Buffer`，自动取它的 `access_ptr("r")` 读指针。

#### 4.2.3 源码精读

前端定义在 [tilelang/language/ascend.py:451-479](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L451-L479)：

```python
def printf(format_str: str, *args):
    format_str = format_str.replace("%p", "0x%x")
    escaped_format = format_str.encode("unicode_escape").decode("utf-8")
    args_list = list(args)
    for i in range(len(args_list)):
        if isinstance(args_list[i], Buffer):
            args_list[i] = args_list[i].access_ptr("r")
        if isinstance(args_list[i], str):
            args_list[i] = args_list[i].encode("unicode_escape").decode("utf-8")
    ...
    return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_printf"), *all_args)
```

codegen 把这个 intrinsic 翻成两条方言，分发处见 [src/target/codegen_ascend.cc:662-663](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L662-L663)（ascendc → `AscendC::PRINTF`）和 [src/target/codegen_ascend_pto.cc:1044-1045](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1044-L1045)（pto → `cce::printf`）。pto 侧的打印实现极简，就是把参数原样透传，见 [src/target/codegen_ascend_pto.cc:2607-2618](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L2607-L2618)。

支持的格式说明符（来自 [examples/print/README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/README.md)）：

| 说明符 | 输出 |
|--------|------|
| `%d` / `%i` | 十进制整数 |
| `%f` | 浮点数 |
| `%x` | 十六进制整数（推荐用于打印地址） |
| `%s` | 字符串 |
| `%p` | 指针地址（前端会自动改写为 `0x%x`） |

#### 4.2.4 代码实践

1. **实践目标**：用 `T.printf` 观察 kernel 里每个核拿到的 `cid`、`vid`，确认 block 切分正确。
2. **操作步骤**：直接运行打印示例 `python examples/print/elementwise_print.py`（该文件已设置 `TL_PTO_DEBUG=1`）。重点关注 [examples/print/elementwise_print.py:48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L48) 这一行：
   ```python
   T.printf("-----cid:%d-------------vid:%d--------------------------------------\n", cid, vid)
   ```
3. **需要观察的现象**：输出里会出现多段形如 `-----cid:0-------------vid:0---...` 的设备端打印，每段对应一个 (cid, vid) 组合执行到该处。
4. **预期结果**：由于 `M=2, N=16, block_M=2, block_N=16`，`m_num*n_num=1`，但 `vid∈{0,1}`，所以能看到 cid 固定、vid 在 0/1 之间各打印一次。**待本地验证**：实际打印条数与核数一致。
5. 若无硬件，可改为用 `func.get_kernel_source()` 查看 `T.printf` 被翻译成了 `cce::printf(...)` 或 `AscendC::PRINTF(...)`，验证前端→codegen 链路正确。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `T.printf` 里直接写 `%p` 也能用？
**参考答案**：前端 `printf` 在 [ascend.py:467](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L467) 把所有 `%p` 自动替换成 `0x%x`，官方也推荐用 `%x` 打印地址。

**练习 2**：`T.printf` 和 Python 的 `print` 有何本质区别？
**参考答案**：`T.printf` 翻译成 TIR intrinsic、最终在 AI Core（设备端）执行，每个核都会打印；Python `print` 在 host（CPU）执行，只打印一次。混用会导致「想在核内看变量却用了 `print`」这类错误。

---

### 4.3 T.dump_tensor：转储整块 buffer

#### 4.3.1 概念说明

`T.printf` 适合打标量（cid、vid、某个地址），但算子调试更常见的需求是「**这块 buffer 里的数到底对不对**」——搬运之后 A_L1 是不是我期望的那片数据？累加之后 C_L0 的值合理吗？softmax 中间结果有没有溢出？`T.dump_tensor` 就是干这个的：把一整块 UB / L1 / L0C / GM buffer 的内容打印出来。

它和 `T.printf` 的关系：`dump_tensor` 专用于「整块张量」，且会在输出开头自动附加丰富的元信息（CANN 版本、核类型、数据类型、所在存储位置等）；`printf` 则是通用的格式化打印。

#### 4.3.2 核心流程

```text
T.dump_tensor(buf, desc, dump_size, shape_info)
   │  buf:      任意 buffer（UB/L1/L0C/GM 均可，无需区分）
   │  desc:     用户自定义编号（uint32），如行号，便于在输出里定位
   │  dump_size:要转储的元素个数
   │  shape_info:可选，按该形状格式化输出
   ▼
tir.call_intrin("tl.ascend_dump_tensor", buf_ptr, desc, dump_size, len(shape_info), *shape_info)
   │
   ├── ascendc ──▶ tl::ascend::DumpTensor ──▶ AscendC::DumpTensor
   └── pto     ──▶ tl::ascend_pto::DumpTensor ──▶ cce::printf + TPRINT
```

`shape_info` 的两条规则（来自 [docs/tutorials/print.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/print.md)）：

- shape 体积 **大于** `dump_size`：按 shape 输出，缺失位置显示 `-`。
- shape 体积 **小于等于** `dump_size`：按 shape 输出，超出 shape 的多余 dump 数据不显示。

#### 4.3.3 源码精读

前端定义在 [tilelang/language/ascend.py:499-532](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L499-L532)，注意它对 `desc` 做了 uint32 校验、并把 buffer 转成读指针：

```python
def dump_tensor(tensor: Buffer, desc: int, dump_size: int, shape_info: tuple = ()):
    if not isinstance(desc, int) or desc < 0 or desc > 0xFFFFFFFF:
        raise ValueError(f"desc must be uint32, but your desc is {desc}")
    tensor_ptr = tensor.access_ptr("r")
    return T.call_intrin(
        "handle",
        tir.op.Op.get("tl.ascend_dump_tensor"),
        tensor_ptr, desc, dump_size, len(shape_info), *shape_info,
    )
```

codegen 在 ascendc 侧把 shape 拼成 `(uint32_t[]){...}` 数组传给模板，见 [src/target/codegen_ascend.cc:2531-2561](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2531-L2561)，并在用到时 `#include "tl_templates/ascend/printf.h"`（[L2532](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2532)）；pto 侧逻辑对称，见 [src/target/codegen_ascend_pto.cc:2620-2661](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L2620-L2661)。

两条模板路线的实现差异是本模块最关键的一点：

- **ascendc** 的 [src/tl_templates/ascend/printf.h:16-36](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/printf.h#L16-L36) 是一层**薄包装**，直接调 CANN 内建的 `AscendC::DumpTensor`，对 `LocalTensor`（片上）和 `GlobalTensor`（GM）各有一个重载，**没有任何 `_DEBUG` 宏门控**——调用永远会被生成出来。
- **pto** 的 [src/tl_templates/pto/printf.h:21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h#L21) 用 `#if defined(_DEBUG) || defined(__CPU_SIM)` 门控：开了 `_DEBUG`（或仿真态的 `__CPU_SIM`）才有真实实现（[L53-97](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h#L53-L97)），否则是空函数（[L99-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h#L99-L108)）——**编译时就被剔成 no-op**。这正是下一模块 `TL_PTO_DEBUG` 存在的根因。

输出会自动带一段头信息（来自 [examples/print/README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/README.md)），形如：

```text
opType=AddCustom, DumpHead: AIV-0, CoreType=AIV, block dim=8, ...
CANN Version: XX.XX, TimeStamp: XXXXXXXXXXXXXXXXX
DumpTensor: desc=111, addr=0, data_type=float16, position=UB, dump_size=32
```

#### 4.3.4 代码实践

1. **实践目标**：用 `T.dump_tensor` 观察 bitwise_and 运算前后 UB buffer 的内容变化。
2. **操作步骤**：运行 `python examples/print/elementwise_print.py`，对照源码 [examples/print/elementwise_print.py:52-53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L52-L53)（运算前 dump `c_ub`）与 [examples/print/elementwise_print.py:70-71](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L70-L71)（运算后 dump `c_ub`）：
   ```python
   T.dump_tensor(c_ub, 222, block_M // VEC_NUM * block_N, (block_M // VEC_NUM, block_N))
   ```
3. **需要观察的现象**：运算前的 `c_ub` 是未初始化的随机值；运算后 `c_ub` 的每个元素应为 `a & b`。由于 `a=1`、`b=2`（int16），`a & b = 0`，所以运算后应全为 0。
4. **预期结果**：两段 dump 输出对比清晰，`desc=222` 帮你在多块 dump 中区分先后；同时文件里对输入 A、B 的 dump（[L42](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L42)、[L44](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L44)）也能看到 A 全 1、B 全 2。**待本地验证**：dump 总量受 1 MB 上限约束（见 4.4）。

#### 4.3.5 小练习与答案

**练习 1**：`dump_tensor` 的 `desc` 参数有什么用？取值有什么限制？
**参考答案**：`desc` 是用户自定义编号（比如源码行号），用来在大量 dump 输出里快速识别某一条；它必须是 uint32（`0 ≤ desc ≤ 0xFFFFFFFF`），否则前端在 [ascend.py:518-519](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L518-L519) 直接抛 `ValueError`。

**练习 2**：pto 路线下，不开 `TL_PTO_DEBUG` 时 `T.dump_tensor` 会怎样？
**参考答案**：pto 的 [printf.h:99-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h#L99-L108) 在 `!(_DEBUG || __CPU_SIM)` 分支里是空函数体，所以调用会被编译成 no-op，运行时**什么都不会打印**，也不产生性能开销。

---

### 4.4 TL_PTO_DEBUG：开关、代价与后端差异

#### 4.4.1 概念说明

`T.printf` / `T.dump_tensor` 写在 kernel 里之后，**默认是不会真正打印的**——因为设备端打印会严重拖慢算子，bisheng 默认会把这些调试接口「编译掉」。要让它们在运行时生效，必须先打开 `TL_PTO_DEBUG` 这个总开关。

`TL_PTO_DEBUG=1` 的作用是：让 tile-lang 在调用 bisheng 编译时追加两个标志——`-D_DEBUG`（定义宏，激活 pto 模板里被 `#if defined(_DEBUG)` 门控的打印实现）和 `--cce-enable-print`（让 bisheng 保留而非剔除打印代码）。两者缺一不可。

> 这是本讲最容易踩坑的一点：**`TL_PTO_DEBUG` 只对 pto 后端自动生效**；ascendc 后端不会自动加这两个标志。详见 4.4.3。

#### 4.4.2 核心流程

```text
os.environ["TL_PTO_DEBUG"] = "1"        # 必须在 kernel 编译之前设置
        │
        ▼
normalize_compiler_options()            # pass_config.py: 读环境变量 → pto_debug=True
        │
        ▼
resolve_compile_flags(target="pto", …)  # libgen.py: 仅当 target=="pto" 且 pto_debug
        │                                 #           才追加 -D_DEBUG --cce-enable-print
        ▼
bisheng -xcce … -D_DEBUG --cce-enable-print kernel.cpp   # 编译时保留打印
```

代价与限制（来自 [docs/TileLang-Ascend Programming Guide.md:2294](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2294)）：

- **性能明显下降**：每条 printf / 断言都引入额外的硬件交互开销，**仅供调测**。
- **1 MB 空间上限**：每个核上所有 dump 接口使用的总空间上限为 **1 MB**，超出会截断。
- 因此调测完成后**必须关闭**（不设置或设为其它值）以恢复正常性能。

#### 4.4.3 源码精读

环境变量读取在 [tilelang/transform/pass_config.py:213-215](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L213-L215)，注意它很**宽容**——只有恰好等于 `"1"` 才开启，任何其它值（包括拼写错误）都不会开启，也不会报错：

```python
env_pto_debug = environ.get("TL_PTO_DEBUG")
if env_pto_debug is not None:
    resolved["pto_debug"] = env_pto_debug.strip() == "1"
```

标志生成在 [tilelang/jit/adapter/libgen.py:106-107](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L106-L107)，**关键判断是 `target == "pto"`**：

```python
if target == "pto" and options["pto_debug"]:
    flags += ["-D_DEBUG", "--cce-enable-print"]
```

这条「仅 pto 生效」的行为有专门的回归测试守护，见 [testing/python/language/test_ascend_compile_flags.py:61-62](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_compile_flags.py#L61-L62)：

```python
("pto", False, {"TL_PTO_DEBUG": "1"}, None, ["-O2", "-D_DEBUG", "--cce-enable-print"]),
("ascendc", False, {"TL_PTO_DEBUG": "1"}, None, ["-O2"]),  # AscendC never gets the PTO debug flags
```

也就是说：即便你设了 `TL_PTO_DEBUG=1`，**ascendc 后端解析出的标志里也没有 `-D_DEBUG --cce-enable-print`**。好在 ascendc 的模板（4.3.3）没有 `_DEBUG` 门控，调用会被生成；若要让 ascendc 在运行时也真正输出，可在 `compile_flags` 里手动追加 `["--cce-enable-print"]`（bisheng 对重复标志是 last-wins，caller 标志追加在最后，见 [libgen.py:108-109](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L108-L109)）。官方文档与打印示例默认走的是 pto + `TL_PTO_DEBUG=1` 这条「开箱即用」的路径。

另一个相关细节：pto codegen 在检测到 `dump_tensor` 时，会自动 `#include "tl_templates/pto/printf.h"`（[src/target/codegen_ascend_pto.cc:479-480](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L479-L480)），所以你无需手动管理头文件。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「开关 `TL_PTO_DEBUG` 决定打印是否生效」。
2. **操作步骤**：
   - 复制 `examples/print/elementwise_print.py` 为 `my_print.py`。
   - 对照 [examples/print/elementwise_print.py:25](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L25) 的 `os.environ["TL_PTO_DEBUG"] = "1"`，分别尝试：① 保持原样运行；② 把这行注释掉运行（注意先 `tilelang.cache.clear_cache()` 清缓存，避免命中旧编译产物）。
3. **需要观察的现象**：开启时，stdout 出现大量 `===========A:`、`DumpTensor: desc=...` 设备端打印；关闭时，这些打印**全部消失**，只剩 host 侧的 `init successful!`、`*******c:`、`Kernel Output Match!`。
4. **预期结果**：开关前后行为差异显著，印证「打印代码默认被编译剔除」。**待本地验证**：受 1 MB 上限影响，dump 数据可能被截断。
5. 进阶：用 `func.get_kernel_source()` 对比开关两份生成代码，pto 路线下模板展开一致，但 bisheng 实际编译标志不同（可借助 [test_ascend_compile_flags.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_compile_flags.py) 的逻辑理解标志差异）。

#### 4.4.5 小练习与答案

**练习 1**：为什么设了 `TL_PTO_DEBUG=1`，ascendc 后端的 kernel 仍然看不到打印？该怎么办？
**参考答案**：[libgen.py:106](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L106) 的判断带了 `target == "pto"`，ascendc 不会自动拿到 `-D_DEBUG --cce-enable-print`。可在 `tilelang.jit(..., compile_flags=["--cce-enable-print"])` 里手动追加（caller 标志 last-wins）。或直接用 pto 后端。

**练习 2**：`TL_PTO_DEBUG` 为什么强调「调完必须关」？
**参考答案**：开启后每条 printf/dump 都引入额外硬件交互，明显降低算子性能（[Programming Guide:2294](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2294)），且每核 dump 总量上限 1 MB。它只服务于调测，不应留在生产配置里。

---

### 4.5 msprof op / simulator：性能采集

#### 4.5.1 概念说明

前面三个模块解决「对不对」，本模块解决「快不快」。当算子结果正确但性能不达标时，需要用 CANN 的 **msProf** 工具采集硬件级性能数据，看算子到底卡在哪——是算力打不满（compute bound），还是带宽喂不饱（memory bound），还是流水没有重叠起来。

msProf 提供两种采集方式（来自 [docs/TileLang-Ascend Programming Guide.md:2422-2455](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2422-L2455)）：

| 方式 | 适用场景 | 能看什么 |
|------|----------|----------|
| **msprof op**（上板） | 真实 NPU 环境，快速定位性能瓶颈 | 计算内存热力图、Roofline 瓶颈分析图、Cache 热力图、通算流水图、算子代码热点图 |
| **msprof op simulator**（仿真） | 开发 / 调试阶段，无硬件或需细粒度分析 | 指令流水图、算子代码热点图、内存通路吞吐率波形图 |

两者都用 **MindStudio Insight** 做可视化呈现。

#### 4.5.2 核心流程

```text
# 方式一：上板采集（需真实 NPU）
msprof op --kernel-name="<your_kernel_func_name>" python your_kernel_script.py

# 方式二：仿真采集（开发阶段）
msprof op simulator --soc-version=<ascend_version> \
                    --kernel-name="<your_kernel_func_name>" \
                    python your_kernel_script.py
```

`--kernel-name` 指定要采集的设备函数名（tile-lang 生成的是 `_kernel`）。仿真方式还需指定 `--soc-version`（如 `Ascend910B1`）。

> 与 u7-l5 的 camodel 仿真区分：camodel 是「**让 kernel 能在没有真机时跑通验证正确性**」，速度慢约 1000 倍；而 `msprof op simulator` 是「**采集指令级性能数据做调优**」，二者目的不同。本讲关注后者。

#### 4.5.3 源码精读

msProf 本身是 CANN 自带工具，不在 tile-lang 仓库源码内，但 tile-lang 通过两件事与之配合：

1. **生成带符号的 `.so`**：性能采集依赖符号信息，tile-lang 默认 `-O2` 编译；若需更详细的调试信息，可通过 `compile_flags=["-g"]`（last-wins）追加，对应仿真采集文档建议的「添加 `-g` 省工程调试信息」（[Programming Guide:2446](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2446)）。`compile_flags` 的传递见 [tilelang/jit/__init__.py:37-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L37-L90)（`compile()` 解析后送入 `resolve_compile_flags`）。
2. **msprof 头文件路径**：AOT 打包脚本里会出现 `-I${ASCEND_HOME_PATH}/include/experiment/msprof` 这样的 include 路径（见 `docs/notebook/aot_jit.ipynb`），说明 CANN 的 msprof 头文件位于 `$ASCEND_HOME_PATH/include/experiment/msprof`，集成时需要引用。

调优方法论层面，仓库内的性能优化参考文档（`.agents/skills/tilelang-perf-optimization/references/best-practices/flash_attn_optimize.md`）给出了用 `msprof op` 采集后如何判读的实例：若 `main_kernel` 之外出现明显的 Transpose / Cast / Copy 辅助 kernel，或某些 shape 明显偏慢，即可对照优化表定位——这正是 msprof 的典型用法。

#### 4.5.4 代码实践

1. **实践目标**：用 `msprof op` 给 GEMM 采集一次性能数据。
2. **操作步骤**：
   - 确认已装 CANN 且 `msprof` 可用（`which msprof`）。
   - 上板采集（需真实 NPU）：
     ```bash
     msprof op --kernel-name="_kernel" python examples/gemm/example_gemm.py --m 1024 --n 1024 --k 1024
     ```
   - 若无硬件，用仿真：
     ```bash
     msprof op simulator --soc-version=Ascend910B1 --kernel-name="_kernel" \
         python examples/gemm/example_gemm.py --m 128 --n 256 --k 64
     ```
3. **需要观察的现象**：msprof 会在当前目录生成 `PROF_xxx/` 采集结果目录，内含性能数据文件；用 MindStudio Insight 打开后能看到通算流水图、Roofline 图等。
4. **预期结果**：成功生成采集目录，并在 Insight 里看到 `_kernel` 的耗时分解。**待本地验证**：具体可视化视图与采集耗时取决于硬件与 CANN 版本。
5. 若无硬件也无仿真环境，可改为「源码阅读型实践」：阅读 [Programming Guide:2429-2455](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2429-L2455)，列出 `msprof op` 与 `msprof op simulator` 各自能输出的图形清单及适用阶段。

#### 4.5.5 小练习与答案

**练习 1**：`msprof op` 和 `msprof op simulator` 各适合什么阶段？给出的图形有何不同？
**参考答案**：`msprof op` 是上板验证，适合在真实环境快速定位性能 / 内存瓶颈，输出计算内存热力图、Roofline 图、Cache 热力图、通算流水图、代码热点图；`msprof op simulator` 是仿真验证，适合开发 / 调试阶段做细粒度分析，输出指令流水图、代码热点图、内存通路吞吐率波形图（[Programming Guide:2429-2455](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L2429-L2455)）。

**练习 2**：性能采集时为什么有时要加 `-g`？怎么传给 tile-lang？
**参考答案**：`-g` 让 bisheng 在 `.so` 里保留调试符号，便于 msprof 关联到源码行（代码热点图）。通过 `tilelang.jit(..., compile_flags=["-g"])` 传入；caller 标志追加在框架默认标志之后、bisheng last-wins（[libgen.py:108-109](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L108-L109)）。

---

## 5. 综合实践

把本讲四个工具串成一条完整的「**调试 → 验证 → 调优**」工作流，针对 GEMM 算子完成以下任务：

**背景**：`examples/gemm/example_gemm.py` 计算 `C = A @ B`，其中累加器 `C_L0` 在 L0C 上（[example_gemm.py:38](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L38)），主循环沿 K 分块用 `T.gemm_v0(..., init=(k==0))` 累加（[example_gemm.py:47](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L47)），最后写回 GM（[example_gemm.py:51](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L51)）。

**任务步骤**：

1. **看源码（4.1）**：在 `func(a, b)` 之前插入 `print(func.get_kernel_source(kernel_only=True))`，确认 `T.gemm_v0` 被翻译成了哪个模板调用、`T.copy(C_L0, ...)` 被翻译成了什么搬出指令（提示：L0C→GM 通常走 fixpipe 类指令）。

2. **加 dump（4.2 / 4.3 / 4.4）**：在 [example_gemm.py:47](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L47) 的 `T.gemm_v0` **之后、`T.barrier_all` 之前**，加一行打印累加器：
   ```python
   T.dump_tensor(C_L0, 777, block_M * block_N, (block_M, block_N))
   ```
   并在脚本最前设置（参考 [elementwise_print.py:25](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/print/elementwise_print.py#L25)）：
   ```python
   import os
   os.environ["TL_PTO_DEBUG"] = "1"
   ```
   先用很小的 shape（`--m 128 --n 256 --k 64`，`block_M=128, block_N=256, K_L1=64`，故 K 方向只有 1 段）跑，避免触发 1 MB 上限。

3. **观察与验证**：
   - 观察 `desc=777` 的 dump 输出，确认每块 `C_L0` 的值就是该 block 对应的 `A_block @ B_block`（K 只有一段时无累加，最易核对）。
   - 把 `--k 128`（K 方向变成 2 段）再跑一次，观察第 2 段 dump 是否等于两段矩阵积之和，验证 `init=(k==0)` 的累加语义。
   - 对比开启 / 关闭 `TL_PTO_DEBUG` 的运行耗时，体会「打印的代价」。

4. **性能采集（4.5）**：调通正确性后，**关掉** `TL_PTO_DEBUG`、移除 dump，恢复大 shape，用 `msprof op` 采集：
   ```bash
   msprof op --kernel-name="_kernel" python examples/gemm/example_gemm.py --m 1024 --n 1024 --k 1024
   ```
   用 MindStudio Insight 打开结果，记录 `_kernel` 的总耗时与「通算流水图」，判断该 GEMM 是算力 bound 还是带宽 bound，为 u7-l2 的高性能优化（双缓冲 / kL0Size 调参）提供数据依据。

**预期结果**：你产出三样东西——① 一份带注释的生成源码片段；② dump 验证「K 分段累加正确」的证据；③ 一份 msprof 性能采集报告。这三者构成了一个算子从「写出来」到「调通」到「调快」的完整证据链。

> 全程**不修改 tile-lang 源码**，只在 `examples/` 下新增 / 临时修改你自己的脚本。

## 6. 本讲小结

- **`get_kernel_source()` 是性价比最高的调试起点**：零硬件开销，直接审视 tile-lang 生成的 Ascend C 代码，是定位「编不过 / 生成不对」的第一手证据。
- **`T.printf` 用于设备端标量打印**，`T.dump_tensor` 用于转储整块 UB/L1/L0C/GM buffer；二者都翻译成 TIR intrinsic，再经 codegen 分两路落到 `AscendC::` / `cce::printf`。
- **pto 模板的打印实现受 `_DEBUG` 宏门控**（[pto/printf.h:21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h#L21)），不开 `_DEBUG` 时被编译成 no-op；ascendc 模板无此门控。
- **`TL_PTO_DEBUG=1` 是打印总开关**，但它**只对 pto 后端自动追加** `-D_DEBUG --cce-enable-print`（[libgen.py:106-107](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L106-L107)）；ascendc 需用 `compile_flags` 手动加 `--cce-enable-print`。
- **开启打印有明显性能代价且每核 dump 上限 1 MB**，仅供调测，调完必须关。
- **`msprof op`（上板）/ `msprof op simulator`（仿真）** 是 CANN 提供的算子级性能采集工具，配合 MindStudio Insight 可定位算力 / 带宽瓶颈，是 u7-l2 高性能优化的数据依据。

## 7. 下一步学习建议

- 学完本讲，你已经掌握了「调试 + 采集」工具箱。下一讲 **u7-l5（A5 仿真运行 camodel）** 会讲在没有真实 A5 NPU 时如何用 camodel 软件仿真**跑通并验证** kernel——注意它与本讲的 `msprof op simulator` 目的不同：camodel 解决「能不能跑通」，msprof simulator 解决「跑得快不快」。
- 想把性能采集用进实战，直接进入 **u7-l2（高性能 GEMM 优化）**：那里会用 msprof 数据驱动双缓冲、`kL0Size`、flag 流水的调参决策。
- 想深入「为什么生成的代码长那样」，回看 **u6-l2（双 Codegen）** 与 **u6-l3（tl_templates 模板库）**，对照本讲引用的两个 `printf.h` 模板理解 ascendc / pto 两套抽象的分野。
- 建议继续阅读源码：`examples/print/elementwise_print.py`（唯一可运行打印样例）、`src/tl_templates/pto/printf.h`（看 `_DEBUG` 门控如何把打印剔成 no-op）、`testing/python/language/test_ascend_compile_flags.py`（看 `TL_PTO_DEBUG` 的标志解析回归测试）。
