# 运行第一个算子：Add 示例端到端体验

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 **核函数（Kernel）** 与 **Device 侧执行函数**，并掌握 `@asc.jit` 装饰器的两种用法。
2. 逐行读懂 `examples/01_add/add.py`：多核切分（`block_length`）、分块（`TILE_NUM`/`BUFFER_NUM`）与 `set_flag`/`wait_flag` 手动同步流水。
3. 掌握 `python3 add.py -r Model/NPU -v Ascend910B1` 两种运行模式背后的机制。
4. 会用 `kernel[core_num, stream](...)` 这个「中括号启动语法」把一个 Kernel 下发到设备上执行。

## 2. 前置知识

本讲是第一个「逐行读代码」的讲义，需要以下背景概念。它们都来自前面的讲义，这里用更具体的方式再讲一遍。

### 2.1 昇腾 AI 核的存储层次：GM 与 UB

写 CUDA 时你会区分显存和 shared memory；写昇腾算子时对应的概念是：

- **GM（Global Memory）**：整片芯片共享的大容量外部存储，Host 侧传入的 torch.Tensor 数据就放在这里。容量大、带宽相对低。
- **UB（Unified Buffer，统一缓冲区）**：每个 AI Core **私有**的小容量高速存储。计算单元（向量/矩阵单元）只能直接读写 UB。

因此一个算子的典型形态是「三段式流水」：

```text
GM --(搬入)--> UB --(计算)--> UB --(搬出)--> GM
     MTE2 流水线      V 流水线        MTE3 流水线
```

### 2.2 三条硬件流水线：MTE2、V、MTE3

昇腾 AI Core 内部有多条**并行工作**的指令流水线，本讲涉及三条：

| 流水线 | 职责 | 类比 |
|--------|------|------|
| MTE2 | 数据搬入（GM → UB） | DMA 读 |
| V | 向量计算（UB 内） | 计算单元 |
| MTE3 | 数据搬出（UB → GM） | DMA 写 |

关键点：这三条流水线**异步并发**。软件发出一条搬入指令后，不必等它完成就可以继续发出计算指令。好处是能做流水线重叠（搬第 2 块数据的同时算第 1 块），代价是**必须由程序员保证顺序**——这就引出了本讲主角之一：`set_flag`/`wait_flag` 同步原语。

### 2.3 多核切分（tiling）与块（Block）

一片 910B 芯片有几十个 AI Core。一个算子跑起来时，Host 会把任务切成多份，每个核（每个 block）处理自己那一份。核内用 `asc.get_block_idx()` 拿到自己的编号，自己算自己负责的数据区间。

### 2.4 Host 侧与 Device 侧

- **Host 侧**：普通 Python 代码，在你的 CPU 上执行，负责准备数据、触发编译、下发任务。
- **Device 侧**：被 `@asc.jit` 修饰的函数体，它**不会**在你 import 时执行 Python 语义，而是被编译成在 NPU 上跑的机器码。

### 2.5 环境

本讲实践只需 u1-l2 完成的源码安装（`pip install -e .`）加 PyTorch；没有 NPU 时用 **Model（仿真器）模式**即可完整跑通。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `examples/01_add/add.py` | 本讲主角：手动同步流水的 Add 算子，端到端可运行 |
| `examples/README.md` | 示例总览、推荐学习顺序、通用运行命令 |
| `python/asc/runtime/jit.py` | `@asc.jit` 装饰器、`JITFunction`、中括号启动语法 `__getitem__` |
| `python/asc/runtime/config.py` | `Backend`/`Platform` 枚举与 `set_platform` 运行模式切换 |
| `python/asc/runtime/launcher.py` | `LaunchOptions`（core_num、stream 两个启动参数） |
| `python/asc/lib/runtime/interface.py` | aclruntime 的 ctypes 封装：`use_model`/`use_npu`/`current_stream` |
| `python/asc/language/core/tensor.py` | `GlobalTensor`/`LocalTensor` 两个 Tensor 抽象 |
| `python/asc/language/core/ir_value.py` | `GlobalAddress`：Host 传入设备指针在 kernel 内的表示 |
| `python/asc/language/basic/data_copy.py` | `asc.data_copy` 搬运接口 |
| `python/asc/language/basic/block_sync.py` | `asc.set_flag`/`asc.wait_flag` 同步原语 |
| `python/asc/language/basic/sys_var.py` | `asc.get_block_idx()` 等系统变量 |
| `python/asc/language/basic/vec_binary.py` | `asc.add` 等向量二元算子 |
| `python/asc/language/core/enums.py` | `TPosition`、`HardEvent` 等硬件枚举 |

按 u1-l3 建立的「目录镜像」规律，这些 `language/` 下的 Python 接口都一一对应后端的 ASC-IR 定义与 Ascend C 发射实现，本讲先只看 Python 侧。

## 4. 核心概念与源码讲解

### 4.1 `@asc.jit` 核函数：Device 代码的 Python 入口

#### 4.1.1 概念说明

`@asc.jit` 是 pyasc 的总开关：它把一个普通 Python 函数包装成 `JITFunction` 对象。此后这个「函数」有了两种身份：

- **不写中括号**：它是一个待编译对象，可以再传编译选项；
- **写中括号** `kernel[core_num, stream](...)`：它被「启动」，触发「源码 → ASC-IR → Ascend C → 二进制 → 下发执行」的完整流水。

需要区分两个角色：

- **Kernel（核函数）**：被 `kernel[核数, 流](...)` 直接启动的函数。它就是最终在 NPU 上执行的入口，本讲的 `vadd_kernel` 是 Kernel。
- **Device 侧执行函数**：同样被 `@asc.jit` 修饰、但只被其他 kernel/Device 函数**调用**的函数。它不会被单独启动，而是被编译器**内联**进调用者的 kernel 里（`examples/02_add_framework` 中的 `copy_in`/`compute`/`copy_out` 就是这种角色，后续单元细讲）。

一句话：**`@asc.jit` 负责「能被编译」，中括号负责「被启动」；只有 Kernel 会被启动。**

#### 4.1.2 核心流程

```text
@asc.jit                     JITFunction(fn)
   │  装饰时：抓取源码与 AST（见 u3）
   ▼
kernel[core_num, stream]     __getitem__ 解析启动参数 → LaunchOptions
   │  返回 _run 可调用对象
   ▼
kernel[core_num, stream](x, y, z, block_length)
   │  _run：绑定参数 → 查缓存 → codegen → compile → launch
   ▼
NPU / 仿真器上执行
```

#### 4.1.3 源码精读

Add 示例中 Kernel 的声明只有两行——装饰器加带类型标注的函数签名：

[examples/01_add/add.py:28-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L28-L29) 用 `@asc.jit` 修饰 `vadd_kernel`；三个输入输出参数都标注为 `asc.GlobalAddress`（Host 传入的设备指针），`block_length` 是运行时 `int` 参数。

`asc.jit` 本身是个很薄的工厂函数，支持「不带参数」「带编译选项」两种用法：

[python/asc/runtime/jit.py:228-235](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L228-L235) `jit()` 在 `fn is None` 时返回 decorator（对应 `@asc.jit(debug=True)` 写法），否则直接包装（对应 `@asc.jit` 写法），最终都构造一个 `JITFunction`。

构造时会做两项校验（参数名不得与配置关键字冲突、选项名必须合法），并预留三个可替换的类属性：

[python/asc/runtime/jit.py:30-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L30-L46) `JITFunction` 用类属性 `codegen`/`compiler`/`launcher` 组合三个执行阶段，`__init__` 中完成选项校验并初始化 `kernel_cache`。

参数里的 `x: asc.GlobalAddress` 为什么在 kernel 内还能有 `.dtype`？因为 JIT 在调用时读取实参的真实类型：

[python/asc/runtime/jit.py:80-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L80-L86) `get_arg_type` 遇到 `torch.Tensor` 时取其 dtype 字符串（去掉 `torch.` 前缀）构造 `PointerArgType`——所以示例第 41 行 `data_type = x.dtype` 拿到的就是 Host 侧那个 float32 张量的类型，无需在 kernel 里重复声明。

#### 4.1.4 代码实践

1. **实践目标**：验证「被 `@asc.jit` 修饰的函数体并不按 Python 语义执行」。
2. **操作步骤**：
   - 打开 `examples/01_add/add.py`，在 `vadd_kernel` 函数体第一行加一句 `print("hello from kernel")`；
   - 以 Model 模式运行示例（运行方式见 4.4.4）。
3. **需要观察的现象**：最终结果 `z == x + y` 依然正确，但终端**不会**打印 `hello from kernel`。
4. **预期结果**：函数体被当作源码编译成设备代码，`print` 这类 Python 内置副作用不会出现在生成的 Kernel 里（pyasc 对允许的内置函数有白名单约束）。观察完请把这行删掉，恢复原样。
5. 终端输出的具体形态「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`@asc.jit` 和 `@asc.jit(always_compile=True)`（`always_compile` 是 `CompileOptions` 的字段）在写法上有什么区别？

**答案**：前者 `fn` 直接传给 `jit()`，返回 `JITFunction`；后者先传关键字参数，`jit()` 返回一个 decorator 再作用于函数（见 [python/asc/runtime/jit.py:228-235](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L228-L235) 的两个分支）。语义上后者每次调用都强制重新编译，不走缓存。

**练习 2**：为什么 `vadd_kernel` 的参数名不能叫 `core_num` 或 `stream`？

**答案**：这些名字属于 JIT 配置关键字（`CodegenOptions`/`CompileOptions`/`LaunchOptions` 的字段集合），与函数参数同名会引起歧义。`JITFunction.__init__` 会调用 `get_clashed_args` 检查并直接抛 `RuntimeError`（[python/asc/runtime/jit.py:37-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L37-L43)）。

### 4.2 `GlobalTensor` 与 `LocalTensor`：两级存储与多核切分

#### 4.2.1 概念说明

pyasc 用两个类把 2.1 节的存储层次映射到 Python：

- **`GlobalTensor`**：GM 上的一段数据的句柄。它**不分配内存**，只是用 `set_global_buffer` 把自己绑定到 Host 传入的设备地址上。
- **`LocalTensor`**：UB 上的一段数据的句柄。构造时指定**逻辑位置**（`TPosition`）、**起始偏移 addr**（字节）与**长度 tile_size**（元素个数）。

`TPosition`（[python/asc/language/core/enums.py:248-261](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L248-L261)）描述 LocalTensor 在核内流水中的逻辑角色：`VECIN` 表示「搬入目的」，`VECOUT` 表示「搬出来源」，`VECCALC` 表示「中间结果」，其余值（A1/B1/CO1 等）服务矩阵计算单元。Add 示例正好用到前两个。

而 `asc.GlobalAddress`（[python/asc/language/core/ir_value.py:35-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L35-L58)）是「裸设备指针」的包装：kernel 参数 `x` 就是它，`x + offset` 触发 `__add__` 生成一条指针偏移 IR（`emitasc.PtrOffsetOp`），得到新地址。`GlobalTensor.set_global_buffer` 吃的正是这种带类型的地址。

#### 4.2.2 核心流程

Add 的多核切分与分块是一组简单的除法。设总元素数 \( N = 8 \times 2048 = 16384 \)，核数为 \( C \)（`USE_CORE_NUM`），则每核负责：

\[ \text{block\_length} = N / C \]

每核内部再切：`TILE_NUM × BUFFER_NUM` 份，每份元素数：

\[ \text{tile\_length} = \frac{N / C}{\text{TILE\_NUM} \times \text{BUFFER\_NUM}} \]

以默认参数（\( C=8 \)、`TILE_NUM=8`、`BUFFER_NUM=2`）代入：`block_length = 2048`，`tile_length = 128`。以 float32（4 字节）计，一块缓冲 128 元素占 512 字节，`buffer_size = tile_length × BUFFER_NUM × 4 = 1024` 字节。

Device 侧初始化顺序（每核各自执行一遍）：

```text
1. offset = get_block_idx() * block_length      # 我是几号核，就从哪段数据开始
2. 创建 3 个 GlobalTensor 并 set_global_buffer(x + offset, block_length)
3. 算出 tile_length / buffer_size
4. 在 UB 上创建 x_local(VECIN, addr=0)、y_local(VECIN, addr=1024)、
   z_local(VECOUT, addr=2048)，各长 tile_length*BUFFER_NUM 个元素
```

注意三个 LocalTensor 的 `addr` 是**手工排布**的：x 占 `[0, 1024)`，y 占 `[1024, 2048)`，z 占 `[2048, 3072)`，互不重叠。这是「手动风格」的特点——02 示例会交给 `TPipe` 自动管理。

#### 4.2.3 源码精读

三个常量定义了切分粒度：

[examples/01_add/add.py:21-23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L21-L23) `USE_CORE_NUM=8` 用几个核，`BUFFER_NUM=2` 双缓冲（只能取 1 或 2），`TILE_NUM=8` 每核切几段。

多核切分的第一步是「算出自己的起点」：

[examples/01_add/add.py:31-37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31-L37) `get_block_idx()` 取本核编号，乘 `block_length` 得到本核数据段的元素偏移；随后三个 `GlobalTensor` 通过 `set_global_buffer(地址, 长度)` 绑定到 GM 上各自负责的区段。

`get_block_idx` 的实现印证了「Device 侧不执行 Python，而是生成 IR」：

[python/asc/language/basic/sys_var.py:33-36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/sys_var.py#L33-L36) 函数体内只有一句 `create_asc_GetBlockIdxOp`——生成一个 `asc.GetBlockIdx` IR 操作，其真实值要到 Kernel 在设备上运行时才存在。

UB 侧缓冲区的创建：

[examples/01_add/add.py:39-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39-L47) 先算 `tile_length` 和字节单位的 `buffer_size`，再以「dtype + 逻辑位置 + 字节偏移 + 元素长度」四要素创建三个 `LocalTensor`，偏移依次为 0、`buffer_size`、`2×buffer_size`。

这四要素如何变成 IR：

[python/asc/language/core/tensor.py:236-244](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L236-L244) `LocalTensor.__init__` 的这个重载把 `pos`/`addr`/`tile_size` 传给 `create_asc_LocalTensorV2Op`，生成一个带 `TPosition` 属性的 UB 张量声明。

`set_global_buffer` 则是「先创建 GlobalTensor 声明，再绑定地址」两步：

[python/asc/language/core/tensor.py:149-167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L149-L167) 校验 `buffer.dtype` 非空后，依次生成 `create_asc_GlobalTensorOp` 与 `create_asc_GlobalTensorSetGlobalBufferOp`；地址必须携带 dtype（正是 4.2.1 说的带类型 `GlobalAddress`），否则抛 `ValueError`。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证多核切分公式的正确性。
2. **操作步骤**：
   - 打开 Python 交互环境，手动计算：`N = 8*2048`，分别代入 `USE_CORE_NUM = 8 / 4 / 16`，求 `block_length = N // C` 与 `tile_length = block_length // 8 // 2`；
   - 再对照源码公式（[examples/01_add/add.py:39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39) 与 [examples/01_add/add.py:75-76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L75-L76)）核对。
3. **需要观察的现象**：三种核数下 `tile_length` 分别是 128 / 256 / 64，且 `C × block_length` 恒等于 16384。
4. **预期结果**：`16384 % 4 == 0` 且 `16384 % 16 == 0`，所以改核数后整除关系成立、结果仍应正确；若把核数改成不能整除 16384 的值（如 3），尾部数据会被截断丢失，结果错误——这也是综合实践中要避免的坑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `x_local`、`y_local`、`z_local` 的 addr 要错开？如果三个都填 0 会怎样？

**答案**：三者共用同一块 UB，addr 是各自的字节起点，错开才不互相覆盖。都填 0 会让搬入的 y 覆盖 x、写出时又覆盖输入，结果错误。这三个 LocalTensor 是手写排布的，正确性靠程序员保证。

**练习 2**：`GlobalTensor` 和 `LocalTensor` 哪个会「分配」内存？

**答案**：都不直接分配。`GlobalTensor` 只是绑定 Host 传入的 GM 地址；`LocalTensor` 是在核内 UB 上的逻辑声明（位置 + 偏移 + 长度）。真正的物理分配由后端 Pass 与 Ascend C 框架完成（u6-l2 会讲 `HoistUBAllocation`）。

**练习 3**：`z_local` 为什么用 `TPosition.VECOUT` 而不是继续用 `VECIN`？

**答案**：`VECOUT` 标记它是「搬出流水（MTE3）」的数据源，与 `VECIN`（MTE2 搬入目的地）区分，硬件与编译器据此安排队列与同步方向（见 [python/asc/language/core/enums.py:248-261](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L248-L261) 的枚举语义）。

### 4.3 `data_copy` 搬运与 `set_flag`/`wait_flag` 手动同步流水

#### 4.3.1 概念说明

- **`asc.data_copy(dst, src, count)`**：在 GM 与 UB 之间搬运 `count` 个元素。它是「三段式流水」里 MTE2/MTE3 的发起点，本身是异步指令的「提交」。
- **`asc.set_flag(event, event_id)` / `asc.wait_flag(event, event_id)`**：跨流水线的事件同步。`HardEvent.MTE2_V` 读作「MTE2 通知 V」：`set_flag` 由**生产者流水线**在某批指令完成时置位事件，`wait_flag` 让**消费者流水线**阻塞直到该事件置位。同一对 `set/wait` 用相同的 `event_id` 区分不同缓冲。

因为 MTE2、V、MTE3 并发工作，「搬入完成前就计算」「计算未完成就搬出」「上一轮还没搬出就复用缓冲」都是数据竞争，必须用事件约束顺序——这就是「手动同步」的含义。

#### 4.3.2 核心流程

主循环体（循环 `TILE_NUM × BUFFER_NUM = 16` 次）每轮做六件事，`buf_id = i % BUFFER_NUM` 在两块缓冲间交替：

```text
① data_copy(x_local[buf_id 块], x_gm[i 块])     # MTE2 搬入 x
② data_copy(y_local[buf_id 块], y_gm[i 块])     # MTE2 搬入 y
③ set_flag(MTE2_V, buf_id); wait_flag(MTE2_V, buf_id)
                                                # V 等 MTE2 搬完
④ asc.add(z_local[buf_id 块], x_local[..], y_local[..], tile_length)  # V 计算
⑤ set_flag(V_MTE3, buf_id); wait_flag(V_MTE3, buf_id)
                                                # MTE3 等 V 算完
⑥ data_copy(z_gm[i 块], z_local[buf_id 块])     # MTE3 搬出
   set_flag(MTE3_MTE2, buf_id); wait_flag(MTE3_MTE2, buf_id)
                                                # 下一轮 MTE2 等 MTE3 读完这块缓冲
```

三对事件的方向：

| 事件 | 生产者 → 消费者 | 保护什么 |
|------|----------------|----------|
| `MTE2_V` | 搬入 → 计算 | ④ 读到的是①②已写入的数据 |
| `V_MTE3` | 计算 → 搬出 | ⑥ 搬出的是④已完成的结果 |
| `MTE3_MTE2` | 搬出 → 搬入 | 复用同一 buf_id 缓冲前，旧数据已被搬完（第 i 轮与第 i+2 轮共用一块缓冲） |

「双缓冲」的价值由此而来：第 i 轮在 V 里算 buf 0 时，MTE2 可以并行搬运下一轮的 buf 1，两条流水线错开重叠，吞吐近似翻倍。

`tensor[off:]` 切片为每轮取「缓冲内第 buf_id 块」的子张量——只允许 `start` 形式的切片（不带 stop/step）。

#### 4.3.3 源码精读

主循环全貌：

[examples/01_add/add.py:49-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49-L69) 循环 16 次，`buf_id = i % BUFFER_NUM` 在双缓冲间交替；先两路搬入，`MTE2_V` 同步后用 `asc.add` 计算，`V_MTE3` 同步后搬出，最后 `MTE3_MTE2` 保证缓冲可安全复用。注释也点明这些是「same core 内不同流水线之间的同步指令」。

`data_copy` 是一个多重载接口，本例走的「count」这条：

[python/asc/language/basic/data_copy.py:144-156](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L144-L156) 实现函数用 `OverloadDispatcher` 按第三参类型自动选择分支；`count: RuntimeInt` 分支生成 `create_asc_DataCopyL2Op(dst, src, count)`，即「按元素个数搬运」的 IR 操作。三个 overload 声明（GM→UB、UB→UB、UB→GM）见 [python/asc/language/basic/data_copy.py:61-73](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L61-L73)。

同步原语的实现同样是「一行一个 IR 操作」：

[python/asc/language/basic/block_sync.py:35-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py#L35-L51) `set_flag` 生成 `create_asc_SetFlagOp(event, event_id)`，`wait_flag` 生成 `create_asc_WaitFlagOp(event, event_id)`；`event_id` 先经 `materialize_ir_value` 物化成 IR 值（本例中是 `buf_id` 这个循环内变量）。

`HardEvent` 的完整清单（36 个方向）在 [python/asc/language/core/enums.py:86-121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L86-L121)，本例用到 `MTE2_V`（=4）、`V_MTE3`（=7）、`MTE3_MTE2`（=19）三个。

切片如何生成 IR：

[python/asc/language/core/tensor.py:255-268](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L255-L268) `LocalTensor.__getitem__` 只接受 `RuntimeInt` 或「只有 start 的 slice」，生成 `create_asc_LocalTensorSubIndexOp`，返回一个带偏移的新 `LocalTensor`——所以 `x_local[buf_id * tile_length:]` 不复制数据，只是「换个起点」的视图。

计算本体：

[python/asc/language/basic/vec_binary.py:38-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L38-L43) `asc.add(dst, src0, src1, *args)` 把参数交给 `op_impl` 并按 overload 选择 `create_asc_AddL0Op/L1Op/L2Op` 之一；本例第三参是 `tile_length`（count 形态），对应按元素个数的向量加。

#### 4.3.4 代码实践

1. **实践目标**：体会同步指令对正确性的必要性。
2. **操作步骤**：
   - 复制 `examples/01_add/add.py` 为 `add_experiment.py`（放在同目录，不要改原文件）；
   - 方案 A：把第 22 行 `BUFFER_NUM` 改为 `1`，运行并核对 `torch.allclose` 断言、记录耗时；
   - 方案 B：恢复 `BUFFER_NUM = 2`，注释掉第 69 行的 `wait_flag(asc.HardEvent.MTE3_MTE2, buf_id)`，再运行。
3. **需要观察的现象**：方案 A 结果应仍正确（单缓冲无复用竞争），但流水无重叠、耗时可能变长；方案 B 在仿真器上可能报同步校验错误或结果偶发错误（缺少「缓冲复用前搬出完成」的约束，第 i+2 轮搬入会与第 i 轮搬出竞争同一块缓冲）。
4. **预期结果**：方案 A 的耗时变化与方案 B 的具体错误形态「待本地验证」（取决于仿真器对同步冲突的检测行为）。
5. 实验后删除 `add_experiment.py`。

#### 4.3.5 小练习与答案

**练习 1**：`HardEvent.MTE2_V` 这个名字里，谁是通知方、谁是等待方？`set_flag` 和 `wait_flag` 各自属于哪方？

**答案**：命名是「生产者_消费者」：MTE2 通知 V。`set_flag` 写在生产者一侧（搬入指令之后），`wait_flag` 写在消费者一侧（使用数据的指令之前）。二者 `event_id` 必须配对。

**练习 2**：既然 set 完立刻 wait，同步是不是白做了？

**答案**：不是。这三条流水线异步并发，源码顺序不等于完成顺序：`wait_flag` 约束的是 **V 流水线**在执行后续指令前必须等到 MTE2 置位的事件。没有它，V 可能在数据尚未到达 UB 时就开始计算。

**练习 3**：为什么搬出之后还需要 `MTE3_MTE2` 这一对事件？

**答案**：双缓冲下 `buf_id` 每 2 轮复用一次同一块 UB。第 i 轮搬出（MTE3 读缓冲）未完成时，第 i+2 轮的搬入（MTE2 写同一缓冲）不能开始，否则数据被覆盖。`MTE3_MTE2` 正是「搬出 → 搬入」方向的保护。

### 4.4 launch 语法 `kernel[core_num, stream]` 与 Model/NPU 两种运行模式

#### 4.4.1 概念说明

Host 侧触发执行的语法是：

```python
vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length)
```

中括号里是**启动选项**（用几个核、放进哪条流），小括号里是**kernel 实参**。它复用了 Python 的下标协议：`kernel[...]` 触发 `JITFunction.__getitem__`，解析出一个 `LaunchOptions` 后返回内部的 `_run`，随后小括号真正调用 `_run` 完成编译与下发。

运行模式由 `config.set_platform(backend, platform)` 决定：

- **`Model`（仿真器）**：在 CPU 上模拟昇腾芯片行为，无需 NPU，适合开发调试；
- **`NPU`（上板）**：真实硬件执行，性能与行为最真实。

#### 4.4.2 核心流程

```text
python3 add.py -r Model -v Ascend910B1
   │
   ├─ main 解析 -r/-v，校验 Backend/Platform 合法性
   ├─ vadd_custom → config.set_platform(Model, Ascend910B1)
   │     ├─ rt.use_model()            # 切换到仿真器动态库
   │     └─ rt.set_soc_version(...)   # 记录芯片型号
   ├─ 构造 torch 张量（Model 模式下 device="cpu"）
   └─ vadd_launch
         ├─ block_length = numel // USE_CORE_NUM
         ├─ rt.current_stream()       # 惰性初始化 device+stream
         └─ vadd_kernel[8, stream](x, y, z, block_length)
               └─ _run：绑定参数 → 缓存查找 → codegen → compile → launch
```

#### 4.4.3 源码精读

Host 侧启动函数：

[examples/01_add/add.py:72-79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L72-L79) `vadd_launch` 用 `torch.zeros_like` 准备输出、按核数算 `block_length`，然后用中括号传「核数 + 当前流」、小括号传四个实参。这就是 launch 语法的标准形态。

中括号语法的实现只有十行：

[python/asc/runtime/jit.py:48-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L48-L57) `__getitem__` 接受单个 int（只给核数）或 tuple（核数、流等），构造 `LaunchOptions` 挂到实例上，然后返回 `self._run`——所以 `kernel[8, stream](args)` 等价于「先配置启动参数，再立刻调用」。

`LaunchOptions` 只有两个字段：

[python/asc/runtime/launcher.py:48-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L48-L51) `core_num`（默认 0）与 `stream`（默认 None），与小括号里的 kernel 实参完全分离。

`_run` 是编译与执行的汇合点：

[python/asc/runtime/jit.py:204-212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L204-L212) `_run` 合并默认选项、按 `CodegenOptions`/`CompileOptions` 抽取编译配置，用 `inspect.signature` 绑定实参，`split_args` 分流出运行时参数与常量，最后 `_cache_kernel`（查缓存否则编译）+ `_run_launcher`（下发）。

两种运行模式的枚举与切换：

[python/asc/runtime/config.py:15-33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L15-L33) 定义 `Backend`（Model/NPU）与 `Platform`（Ascend910B1 等 12 个型号）两个枚举，是命令行 `-r`/`-v` 的合法取值来源。

[python/asc/runtime/config.py:48-67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L48-L67) `set_platform` 在 Model 分支默认 `Ascend910B1` 并调用 `rt.use_model()`；NPU 分支会核对 `-v` 传入型号与实际芯片一致后调用 `rt.use_npu()`。

`use_model`/`use_npu` 只是设置全局状态里的模式开关：

[python/asc/lib/runtime/interface.py:48-55](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L48-L55) 两个函数分别把 `state.model` 置 True/False——后续加载哪套 aclruntime 动态库（真硬件 or 仿真器）由此决定。

`rt.current_stream()` 的惰性初始化：

[python/asc/lib/runtime/interface.py:124-126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L124-L126) `current_stream` 内部先 `_lazy_init()`（首次调用时加载库、设置默认 device、创建流），再返回当前 device 的流对象。

命令行解析与校验：

[examples/01_add/add.py:92-109](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L92-L109) `main` 用 argparse 定义 `-r`（默认 Model）与 `-v`（默认 None），校验取值合法后调用 `vadd_custom`，跑通后打印 success 日志。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：端到端跑通 Add 示例，并观察核数对编译/执行的影响。
2. **操作步骤**：
   - 前置：已完成 u1-l2 的 `pip install -e .`，且 `python3 -c "import torch"` 可用；
   - Model 模式需要仿真器库（见 [docs/quick_start.md:325-327](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/quick_start.md#L325-L327)）：
     ```bash
     export LD_LIBRARY_PATH=$ASCEND_HOME_PATH/tools/simulator/Ascend910B1/lib:$LD_LIBRARY_PATH
     ```
   - 在仓库根目录运行：
     ```bash
     python3 examples/01_add/add.py -r Model -v Ascend910B1
     ```
   - 把 [examples/01_add/add.py:21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L21) 的 `USE_CORE_NUM` 分别改为 `4` 和 `16`，各重新运行一次（`16384` 能被两者整除）；
   - 如有 NPU 环境，再对比 `python3 examples/01_add/add.py -r NPU -v Ascend910C`。
3. **需要观察的现象**：
   - 三种核数下都应输出 `Sample add run success.`（`torch.allclose(z, x+y)` 断言通过，见 [examples/01_add/add.py:89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L89)）；
   - 每个核数首跑会触发编译（较慢），同参数重复运行会命中缓存明显变快；改核数不影响缓存 key 中的 constexpr/参数类型部分，但执行阶段任务切分不同。
4. **预期结果**：核数 4/8/16 的数值结果完全一致；编译耗时与执行耗时的具体差值「待本地验证」。注意改核数为不整除 16384 的值（如 3）会使 `block_length` 向下取整、丢尾部数据，断言失败。
5. 运行命令的通用形式见 [examples/README.md:39-55](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/README.md#L39-L55)。

#### 4.4.5 小练习与答案

**练习 1**：`vadd_kernel[8, stream](x, y, z, block_length)` 里，中括号与小括号的职责分别是什么？

**答案**：中括号经 `__getitem__` 生成 `LaunchOptions(core_num=8, stream=stream)`（[python/asc/runtime/jit.py:48-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L48-L57)），决定「怎么跑」；小括号是 kernel 实参，进入 `_run` 的参数绑定与缓存 key 计算（[python/asc/runtime/jit.py:204-212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L204-L212)），决定「算什么」。

**练习 2**：`-v Ascend910B1` 在 Model 模式和 NPU 模式下的校验行为有何不同？

**答案**：Model 模式下 `-v` 指定要仿真的芯片型号（不传默认 `Ascend910B1`）；NPU 模式下运行时读取真实芯片型号，若与传入值不一致会抛 `ValueError`（[python/asc/runtime/config.py:55-64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L55-L64)）。

**练习 3**：Model 模式下 `x = torch.rand(..., device="cpu")`，Kernel 是怎么拿到数据的？

**答案**：示例在 [examples/01_add/add.py:84-87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L84-L87) 依据后端选择 device；张量经 `get_arg_type` 识别为 `PointerArgType` 后，由 Launcher 侧的 `MemoryHandle` 负责 Host/Device 间的数据搬运与回收（详见 u3-l6），仿真器模式下数据实际留在 Host 内存中由模拟器访问。

## 5. 综合实践

**任务：把 Add 示例改造成「乘加」算子并在三种核数下验证。** 这个小任务串起本讲全部四个模块：`@asc.jit`、两级 Tensor、搬运与手动同步、launch 语法。

1. **准备**：复制 `examples/01_add/add.py` 到同目录 `muladd.py`（保持原文件不动）。
2. **改造 kernel**：参考 [python/asc/language/basic/vec_binary.py:281-299](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L281-L299) 中 `asc.mul` 的签名（与 `asc.add` 同形：`dst, src0, src1, count`），把循环内的计算行替换为「先乘后加」两步：
   ```python
   asc.mul(t_local[buf_id * tile_length:], x_local[buf_id * tile_length:],
           y_local[buf_id * tile_length:], tile_length)
   asc.add(z_local[buf_id * tile_length:], t_local[buf_id * tile_length:],
           z_local[buf_id * tile_length:], tile_length)  # z = z + t
   ```
   其中 `t_local` 是你新加的一个 `asc.LocalTensor(data_type, asc.TPosition.VECCALC, 起始偏移, tile_length * BUFFER_NUM)`，起始偏移应排在现有三块缓冲之后（即 `3 * buffer_size` 处，避免覆盖）。两步计算之间与之后按 4.3 节的规则补齐必要的 `set_flag`/`wait_flag`（先想清楚：V → V 之间是否需要事件？`V_MTE3` 应该移到哪里？）。
3. **改造 Host**：把 `vadd_custom` 里的断言改为 `assert torch.allclose(z, x * y + z_ref)`（注意先用原始 `z`（全 0）做参考会得到 `x*y`，请自行设计正确的参考值，例如 `assert torch.allclose(z, x * y)`，取决于你的加法写法）。
4. **验证**：分别在 `USE_CORE_NUM = 8 / 4 / 16` 下以 `python3 muladd.py -r Model -v Ascend910B1` 运行，三种核数结果应一致且与参考值吻合。
5. **思考题**（写在实验记录里）：新增一个中间缓冲后，`MTE3_MTE2` 的保护范围是否需要变化？为什么？
6. 运行耗时与 IR 具体形态「待本地验证」；完成后删除 `muladd.py`。

## 6. 本讲小结

- `@asc.jit` 把 Python 函数包装成 `JITFunction`，函数体被编译为设备代码而非按 Python 语义执行；被 `kernel[核数, 流](...)` 启动的是 **Kernel**，被其他 jit 函数调用并内联的是 **Device 侧执行函数**。
- `GlobalTensor` 绑定 GM 地址（`set_global_buffer`），`LocalTensor` 在 UB 上按「dtype + TPosition + 字节偏移 + 长度」声明；Add 示例的三个 UB 缓冲由程序员手工排布。
- 多核切分是两层除法：`block_length = N / 核数`，`tile_length = block_length / (TILE_NUM × BUFFER_NUM)`；默认参数下每核 2048 个元素、每次迭代搬运 128 个。
- 搬入（MTE2）、计算（V）、搬出（MTE3）三条流水线异步并发，需要 `set_flag`/`wait_flag` 按 `MTE2_V`、`V_MTE3`、`MTE3_MTE2` 三个方向手动配对同步；双缓冲（`BUFFER_NUM=2`）让相邻迭代的搬运与计算重叠。
- launch 语法的中括号经 `__getitem__` 变成 `LaunchOptions(core_num, stream)`，小括号实参进入 `_run` 的绑定与缓存 key 计算；`-r Model`（仿真器，配 `LD_LIBRARY_PATH`）与 `-r NPU`（真机，校验型号）两种模式由 `config.set_platform` 切换。

## 7. 下一步学习建议

- **下一讲（u1-l5）**：设置 `PYASC_DUMP_PATH` 重跑 Add，导出 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp`，把本讲的每个 `asc.xxx` 调用与中间产物一一对应，建立「一次 JIT 调用」的全链路地图。
- **横向对比**：阅读 `examples/02_add_framework/add_framework.py`，看 `TPipe`/`TQue` 如何接管本讲手工完成的缓冲排布与同步插入（u2-l6 细讲），体会两种风格的取舍。
- **源码延伸**：通读 [python/asc/language/basic/block_sync.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py) 中其余同步原语（`pipe_barrier`、`sync_all`、`cross_core_set_flag`），了解核内与跨核同步的完整工具箱。
