# TPipe/TQue/TBuf：框架化的内存管理与自动同步

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 `TPipe.init_buffer` + `TQue` 双缓冲的标准编程范式，理解「一个 Kernel 只有一个 TPipe、若干 TQue/TBuf」的资源组织方式。
2. 说清 `alloc_tensor → enque → deque → free_tensor` 四步生命周期各自的含义、产生的 IR 操作，以及它们与流水线同步的关系。
3. 区分 `TQue`、`TBuf`、`TQueBind` 三者的使用场景：普通队列、临时缓冲、跨逻辑位置的特殊数据通路。
4. 对比 `01_add`（手动 LocalTensor + set_flag/wait_flag）与 `02_add_framework`（TPipe/TQue 框架）两种编程风格，理解框架化如何把「内存手工排布 + 手动同步」两项易错工作接管过来。

本讲是第 2 单元（language 层用户接口）的收官：前面五讲讲的 `LocalTensor`、`HardEvent`、`data_copy` 都是「散装零件」，本讲的 TPipe 框架是把零件组装成「流水线」的标准脚手架。

## 2. 前置知识

阅读本讲前，请确认已理解以下概念（均在前面讲义中讲过，此处一句话回顾）：

- **GM 与 UB 两级存储**：`GlobalTensor` 是 Global Memory 的视图，`LocalTensor` 描述 Unified Buffer 上的一段内存（u1-l4、u2-l2）。
- **三条异步流水线**：MTE2（搬入）、V（矢量计算）、MTE3（搬出）并发执行，需要 `set_flag/wait_flag` 按 `HardEvent` 方向配对同步（u2-l4）。
- **JIT 编译期的 Python 执行**：`@asc.jit` 函数体在 JIT 编译期被逐行翻译成 IR，`asc.TPipe()` 这类「构造函数调用」实际生成的是 IR 操作，而不是真的在 Host 上分配内存（u1-l5、u2-l3）。
- **OverloadDispatcher 与 require_jit**：pyasc 用注册式重载分发弥补 Python 无静态重载，用 `require_jit` 把 API 约束在 JIT 编译期调用（u2-l5）。
- **PYASC_DUMP_PATH**：设置该环境变量可导出 `codegen.mlir`（Pass 前 IR）、`ascir.mlir`（Pass 后 IR）、`ascendc.cpp` 等中间产物（u1-l5）。

一个形象类比：手动风格像「自己搭帐篷」——自己算地址偏移、自己看着流水线打旗语；框架风格像「住酒店」——告诉前台（TPipe）要几间房（init_buffer），房卡（alloc_tensor）进出（enque/deque）都有人管秩序。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/language/fwk/tpipe.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py) | 本讲主文件：`TQueBind`、`TBuf`、`TBufPool`、`TPipe`、`TPipeManager`、`get_tpipe_ptr`、`TQue` 全部在此 |
| [python/asc/language/fwk/\_\_init\_\_.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/__init__.py) | fwk 子包出口，导出 6 个公开名字 |
| [python/asc/language/fwk/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/utils.py) | `set_tpipe_docstring` 装饰器与文档表，收录每个接口对应的 Ascend C 原型、约束与示例，是「权威参数说明」的所在地 |
| [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py) | 框架风格参考实现：TPipe + 三个 TQue + copy_in/compute/copy_out 三段式 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 手动风格对照实现：LocalTensor 手工排布 + 三对手动同步 |
| [include/ascir/Dialect/Asc/IR/Fwk/TQue.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td) | 队列相关 IR 操作的 TableGen 定义（alloc/deque/enque/free） |
| [include/ascir/Dialect/Asc/IR/Fwk/TBuf.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TBuf.td) | `asc.tbuf` / `asc.tbuf.get_tensor` 等 IR 操作定义 |
| [include/ascir/Dialect/Asc/IR/Fwk/TPipe.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td) | `asc.pipe`、`asc.pipe.init_buffer`、`asc.pipe.init_queue` 等 IR 操作定义 |
| [python/src/IR.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp) | pybind 桥接层，`need_insert_sync` 的判定在此 |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | Pass 调度器，`insert_sync` 选项触发的同步重建 Pass 链在此 |
| [python/test/unit/language/fwk/test_tpipe.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/fwk/test_tpipe.py) | TPipe 接口的单元测试，可作为最小用例模板 |

类继承关系一览（对应 tpipe.py 中的定义顺序）：

```text
IRValue（抽象协议：from_ir / to_ir，见 u2-l3）
 ├── TQueBind   （L23）  绑定 src→dst 两个逻辑位置的队列基类
 │    ├── TBuf  （L197） 临时缓冲：get / get_with_offset
 │    └── TQue  （L540） 普通队列：TQueBind 的简化模式
 ├── TBufHandle （L270） 缓冲块裸句柄（配合 TBufPool 使用）
 ├── TBufPool   （L285） 手动内存池（进阶，本讲只做了解）
 └── TPipe      （L365） 内存与同步事件总管
```

## 4. 核心概念与源码讲解

### 4.1 TPipe：Device 内存与同步事件的总管

#### 4.1.1 概念说明

一个 Kernel 函数里的所有 `LocalTensor` 都要落在 UB（或 L1 等片上存储）上，手动风格里由你自己算偏移；框架风格里，这块「谁用哪段内存」的账本交给 `TPipe` 统一记录。`TPipe` 的职责有两个：

1. **内存资源管理**：通过 `init_buffer` 为 `TQue`（按块数 × 块大小）和 `TBuf`（按总字节数）划分内存。
2. **同步事件管理**：通过 `alloc_event_id / fetch_event_id / release_event_id` 申请、获取、释放事件 ID，供手动 `set_flag/wait_flag` 场景使用。

约束非常严格：**一个 Kernel 内全局只能存在一个 TPipe 实例**，这一点在 Python 前端就被强制检查了（见下文 `TPipeManager`）。

#### 4.1.2 核心流程

在 Kernel 中使用 TPipe 的标准时序：

```text
pipe = asc.TPipe()                      # ① 创建 asc.pipe 操作，并登记为全局唯一实例
que  = asc.TQue(VECIN, BUFFER_NUM)      # ② 创建队列（框架对象，尚无内存）
pipe.init_buffer(que, num, len)         # ③ 划分内存：num 块 × len 字节
...                                     # ④ 业务代码用 que.alloc_tensor/deque 等
（Kernel 结束）                          # ⑤ JIT teardown 时 TPipeManager 自动复位
```

内存划分数值上就是：

\[ \text{该队列占用的字节数} = \text{num} \times \text{len} \]

其中 `len` 通常写成 `tile_length * dtype.sizeof()`（元素数 × 每元素字节数），与 01_add 手工计算的 `tile_length * BUFFER_NUM * data_type.sizeof()`（add.py 第 42 行）总量一致——只是框架替你把「块数」与「偏移」管了起来，且非 32 字节对齐时 API 内部会自动向上补齐。

#### 4.1.3 源码精读

**TPipe 构造与全局唯一性**。[python/asc/language/fwk/tpipe.py:382-388](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L382-L388)：构造函数只做两件事——调用 `create_asc_PipeOp()` 生成 `asc.pipe` IR 操作，然后 `TPipeManager.set(self)` 把自己登记为全局唯一实例。

```python
def __init__(self, handle: Optional[IRHandle] = None) -> None:
    if handle is not None:
        self.handle = handle
        return
    self.handle = global_builder.get_ir_builder().create_asc_PipeOp()
    TPipeManager.set(self)
```

**TPipeManager 的单例检查与自动复位**。[python/asc/language/fwk/tpipe.py:497-515](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L497-L515)：第二次 `TPipe()` 直接抛 `RuntimeError`（「TPipe instance is already created」）；`set` 同时通过 `global_builder.on_teardown(cls.reset)` 注册了一次性清理钩子——每次 JIT 编译结束（teardown）时自动把类变量清空，这样下一个 Kernel 编译时又是干净的。这是「编译期 Python 对象生命周期」与「编译批次」对齐的一个典型设计。

```python
@classmethod
def set(cls, pipe: TPipe) -> None:
    if cls.instance is not None:
        raise RuntimeError("TPipe instance is already created, use get_tpipe_ptr() to obtain it")
    cls.instance = pipe
    global_builder.on_teardown(cls.reset)
```

**get_tpipe_ptr：Device 子函数里取回 pipe**。[python/asc/language/fwk/tpipe.py:518-537](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L518-L537)：对应 Ascend C 的 `GetTPipePtr()`。当 Device 侧执行函数（如 u1-l4 所述，被其他 jit 函数调用并内联的函数）里需要访问 pipe（例如申请 event_id）而不方便通过参数传递时，用它取回全局实例。

**init_buffer 的双重重载**。[python/asc/language/fwk/tpipe.py:463-479](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L463-L479)：用 `OverloadDispatcher`（u2-l5 讲过的注册式重载）区分两种签名——`(que, num, len)` 生成 `asc.pipe.init_queue` 操作，`(buf, len)` 生成 `asc.pipe.init_buffer` 操作。`num` 与 `len` 经 `_mat`（`materialize_ir_value`）物化为 int32 IR 值，因此既接受 Python 立即数也接受运行时整型。

```python
@dispatcher.register(que=TQue, num=RuntimeInt, len=RuntimeInt)
def _(que: TQue, num: RuntimeInt = 0, len: RuntimeInt = 0):
    global_builder.get_ir_builder().create_asc_TPipeInitQueueOp(self.to_ir(), que.to_ir(),
                                                                _mat(num, KnownTypes.int_).to_ir(),
                                                                _mat(len, KnownTypes.int_).to_ir())

@dispatcher.register(buf=TBuf, len=RuntimeInt)
def _(buf: TBuf, len: RuntimeInt = 0):
    global_builder.get_ir_builder().create_asc_TPipeInitBufferOp(self.to_ir(), buf.to_ir(),
                                                                 _mat(len, KnownTypes.int_).to_ir())
```

这两个操作的后端定义在 [include/ascir/Dialect/Asc/IR/Fwk/TPipe.td:34-52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L34-L52)：`AscendC_TPipeInitQueueOp` 的 IR 助记符是 `pipe.init_queue`，但第二个模板参数（API 名）是 `InitBuffer`——即发射到 Ascend C 时统一还原成 `pipe.InitBuffer(que, num, len)`。这是「IR 助记符 ≠ C 函数名」的一个实例，读 dump 时不要混淆。

**事件 ID 三件套**。[python/asc/language/fwk/tpipe.py:397-405](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L397-L405) 的 `alloc_event_id` 返回 `PlainValue`（设备侧运行时才确定值的标量，见 u2-l3），[tpipe.py:481-489](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L481-L489) 的 `release_event_id` 必须与之配对。它们服务于「框架 + 少量手动同步」混合编程的场景——fwk/utils.py 的文档明确要求 alloc/release 成对出现，防止 TEventID 耗尽。

#### 4.1.4 代码实践

**实践目标**：验证 TPipe 的单例约束与 init_buffer 的 IR 生成。

**操作步骤**：

1. 进入仓库根目录，先跑现成单元测试，确认环境可用（无需 NPU，Model 仿真即可）：

   ```bash
   python3 -m pytest python/test/unit/language/fwk/test_tpipe.py -k "init_buffer or init_method" -v
   ```

2. 新建 `my_tpipe_probe.py`（示例代码，非项目文件），写两个最小 kernel：

   ```python
   import asc
   from asc.runtime import config

   config.set_platform(config.Backend.Model, check=False)

   @asc.jit
   def ok_kernel():
       pipe = asc.TPipe()
       que = asc.TQue(asc.TPosition.VECIN, 2)
       buf = asc.TBuf(asc.TPosition.VECCALC)
       pipe.init_buffer(que=que, num=2, len=128)
       pipe.init_buffer(buf=buf, len=256)

   ok_kernel[1]()

   @asc.jit
   def bad_kernel():
       p1 = asc.TPipe()
       p2 = asc.TPipe()   # 故意创建第二个 TPipe

   bad_kernel[1]()
   ```

3. 先只运行 `ok_kernel`，设置 `PYASC_DUMP_PATH=/tmp/dump` 后重跑，打开 `codegen.mlir` 搜索 `asc.pipe`。

**需要观察的现象**：

- `ok_kernel` 正常执行；dump 的 IR 中出现一条 `asc.pipe` 操作、一条 `pipe.init_queue` 和一条 `pipe.init_buffer`。
- 放开 `bad_kernel` 后，JIT 编译期（不是运行期）抛出 `RuntimeError: TPipe instance is already created...`。

**预期结果**：单例检查发生在 Python 前端编译阶段；`ok_kernel` 的行为「待本地验证」（取决于本机是否完成 u1-l2 的源码安装）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TPipeManager.set` 要注册 `global_builder.on_teardown(cls.reset)`？如果不注册会怎样？

**答案**：TPipeManager 的 `instance` 是跨 Kernel 的类变量。不清理的话，上一个 Kernel 编译结束后 `instance` 仍指向旧 pipe，下一个 Kernel 再写 `TPipe()` 会误判「已创建」而抛错；或 Device 子函数里 `get_tpipe_ptr()` 拿到的是上一个 Kernel 的失效对象。注册 teardown 钩子保证「一个编译批次一个 TPipe」。

**练习 2**：`pipe.init_buffer(que, num=2, len=128)` 总共为该队列划出多少字节？若 dtype 是 float32，len 应如何按 tile_length 写出？

**答案**：\(2 \times 128 = 256\) 字节。len 一般写 `tile_length * dtype.sizeof()`，即 \( \text{len} = \text{tile\_length} \times 4 \) 字节；`num` 才承担「块数/双缓冲」的维度。

**练习 3**：TPipe 还提供了哪组接口服务于手动同步？它们与 u2-l4 的 `set_flag/wait_flag` 是什么关系？

**答案**：`alloc_event_id / fetch_event_id / release_event_id`（tpipe.py:397-420、481-489）。它们提供 `set_flag/wait_flag` 所需的 `event_id` 实参——`alloc` 会占用 ID 必须与 `release` 配对，`fetch` 只查询不占用，适合临时使用。

### 4.2 TQue 与 alloc/enque/deque/free 四步生命周期

#### 4.2.1 概念说明

`TQue` 是流水线任务间通信与同步的数据结构，本质是「绑定了一块 UB 内存的队列」：生产者把装好数据的张量「入队」，消费者从队列「出队」使用、用完「释放」。这一入一出天然带了两重保障：

1. **内存互斥**：`alloc_tensor` 拿到的是当前空闲块；块没被 `free_tensor` 之前不会再分配给别人，替代了手动风格里「buf_id = i % BUFFER_NUM 手工轮转」的做法。
2. **隐式同步**：`enque/deque` 映射到 Ascend C 的 `EnQue/DeQue`，同步事件由队列框架在生成的 Ascend C 代码内部处理——这正是 02 示例里一处 `set_flag/wait_flag` 都没有写、结果仍然正确的原因（框架接管的同步属于 Ascend C 类库行为；pyasc 自己「重建同步」的 Pass 链见 4.4.3）。

#### 4.2.2 核心流程

双缓冲流水下，一次循环内四个接口的配合（生产者视角在左，消费者视角在右）：

```text
生产者（copy_in 侧）                 消费者（compute/copy_out 侧）
─────────────────────              ─────────────────────
t = que.alloc_tensor(dtype)  ──►   t = que.deque(dtype)      # 从队列取出一块已就绪的数据
data_copy(t, gm[...])              asc.add(...)  # 用这块数据计算
que.enque(t)                 ──►   que.free_tensor(t)         # 归还内存块，允许生产者复用
        ▲ 数据就绪的通知沿队列传递 ▼
```

四步语义速查：

| 接口 | 语义 | 生成的 IR 操作（IR 助记符） | 对应 Ascend C |
| --- | --- | --- | --- |
| `alloc_tensor(dtype)` | 从队列绑定的缓冲中分一块空闲内存 | `asc.que_bind.alloc_tensor` | `AllocTensor<T>()` |
| `enque(tensor)` | 张量入队（数据就绪通知） | `asc.que_bind.enque_tensor` | `EnQue(tensor)` |
| `deque(dtype)` | 出队取一块已就绪的张量 | `asc.que_bind.deque_tensor` | `DeQue<T>()` |
| `free_tensor(tensor)` | 释放内存块供复用 | `asc.que_bind.free_tensor` | `FreeTensor(tensor)` |

辅助查询接口：`has_idle_buffer` / `has_tensor_in_que` / `vacant_in_que` / `get_tensor_count_in_que`（tpipe.py:152-171、189-194），以及批量释放事件的 `free_all_event`（tpipe.py:141-145）。

#### 4.2.3 源码精读

**TQue 构造**。[python/asc/language/fwk/tpipe.py:554-566](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L554-L566)：`pos`（逻辑位置）与 `depth`（队列深度）都必须是编译期常量（`require_constexpr`），因为它们要进入队列的 IR **类型**（`builder.get_queue_type(pos, depth)`）——这与 u2-l4 讲的「枚举最终成为 C++ 模板参数」一脉相承。随后生成 `asc.queue` 操作，并以 handle 交给父类 `TQueBind` 完成包装：

```python
def __init__(self, pos=..., depth=None, mask=0, handle=None) -> None:
    ...
    require_constexpr(pos, int, arg_name="pos")
    require_constexpr(depth, int, arg_name="depth")
    pos = ConstExpr.unwrap(pos)
    depth = ConstExpr.unwrap(depth)
    builder = global_builder.get_ir_builder()
    ir_type = builder.get_queue_type(pos, depth)
    self.handle = builder.create_asc_QueueOp(ir_type)
    super().__init__(handle=self.handle)
```

对应的 IR 定义 [include/ascir/Dialect/Asc/IR/Fwk/TQue.td:181-185](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L181-L185)：`AscendC_QueueOp` 无参数、结果类型是 `AscendC_Queue`，pos/depth 编码在类型里。所以在 mlir 文本里你会看到类似 `asc.queue : !asc.queue<VECIN, 2>` 的形式（具体打印格式以本地 dump 为准）。

**alloc_tensor 的两种形态**。[python/asc/language/fwk/tpipe.py:70-85](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L70-L85)：`alloc_tensor(dtype)` 返回新建的 `LocalTensor`（non-inplace，要求 depth 非零）；`alloc_tensor(tensor)` 传入既有张量原地绑定（in-place，要求 depth 为 0）。返回值经 `LocalTensor(handle=handle, dtype=dtype, shape=None)` 包装——又是 u2-l2 讲过的「Tensor 不持有数据，只是 dtype + IR 句柄」。

```python
@dispatcher.register(dtype=DataType)
def _(dtype: DataType):
    tensor_type = ir.get_local_tensor_type(dtype.to_ir())
    handle = global_builder.get_ir_builder().create_asc_TQueBindAllocTensorOp(tensor_type, self.to_ir())
    return LocalTensor(handle=handle, dtype=dtype, shape=None)
```

**enque 与 deque**。[python/asc/language/fwk/tpipe.py:124-139](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L124-L139) 与 [tpipe.py:99-122](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L99-L122)：两者都各有「普通 / in-place / 带 src_user_pos+dst_user_pos」三种注册。带位置的变体是给 `TQueBind` 跨位置通路用的（见 4.3），普通 TQue 用第一种即可。注意 `deque(dtype)` 需要显式给 dtype——队列只管内存不记类型，02 示例特意把 `z_gm` 传进 `compute` 就是为了借它的 dtype（add_framework.py 第 65 行注释）。

后端定义见 [include/ascir/Dialect/Asc/IR/Fwk/TQue.td:25-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L25-L32)（AllocTensor）、[TQue.td:43-59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L43-L59)（DeQue 两种）、[TQue.td:80-98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L80-L98)（EnQue 两种）：

```tablegen
def AscendC_TQueBindEnqueTensorOp : APIOp<"que_bind.enque_tensor", "EnQue", [AscMemberFunc]> {
  let summary = "Push tensor back to queue";
  let arguments = (ins AscendC_BaseQueueTypeInterface:$queue,
                       AscendC_LocalTensor:$tensor);
  ...
}
```

读法：操作数是「队列 + 张量」，没有结果——入队是一个副作用操作；`AscMemberFunc` 标记它发射为 C++ 成员函数调用 `queue.EnQue(tensor)`。

**free_tensor**。[python/asc/language/fwk/tpipe.py:147-150](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L147-L150)：一行生成 `asc.que_bind.free_tensor`。02 示例的 `compute` 在计算完立即 `free_tensor(x_local/y_local)`（add_framework.py:71-72）——早释放让输入队列的缓冲尽早可复用，是双缓冲流水的关键节奏点。

**生命周期约束（来自权威文档表）**。[python/asc/language/fwk/utils.py:1755-1785](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/utils.py#L1755-L1785) 的 `set_tpipe_docstring` 把 utils.py 中手写的文档（函数介绍 / C++ 原型 / 参数 / 约束 / 示例）注入每个方法的 `__doc__`。其中 deque 的约束（[utils.py:237-242](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/utils.py#L237-L242)）明确：**对空队列 deque 是异常行为，CPU 调测时会报错**；non-inplace 接口要求 depth 非零，in-place 接口要求 depth 为 0。查接口约束时，这个文件比猜源码可靠。

#### 4.2.4 代码实践

**实践目标**：用 02 示例做「破坏性实验」，体感理解四步生命周期缺一不可。

**操作步骤**：

1. 复制 `examples/02_add_framework/add_framework.py` 为 `add_framework_exp.py`。
2. 实验一（去掉 free）：注释掉 `compute` 里的 `in_queue_x.free_tensor(x_local)` 与 `in_queue_y.free_tensor(y_local)`（add_framework.py:71-72）。
3. 实验二（交换顺序）：在 `copy_in` 里把 `in_queue_x.enque(x_local)` 移到两条 `data_copy` **之前**。
4. 分别运行：`python3 add_framework_exp.py -r Model`。

**需要观察的现象**：

- 实验一：BUFFER_NUM=2 时程序可能仍能跑完（队列有两块，短循环下恰好够用），但把 `TILE_NUM` 调大后，队列缓冲耗尽、行为异常或报错——「待本地验证」。
- 实验二：数据尚未搬入就宣布就绪，计算读到未初始化内存，`torch.allclose` 断言大概率失败——「待本地验证」。

**预期结果**：两个实验都说明同一件事——`enque/deque/free` 的**顺序**承载了正确性语义，框架管同步的前提是你按生命周期约定书写。

#### 4.2.5 小练习与答案

**练习 1**：02 示例中 `TQue(asc.TPosition.VECIN, BUFFER_NUM)` 的第二个参数与 `pipe.init_buffer(que, BUFFER_NUM, ...)` 的第二个参数分别是什么含义？

**答案**：前者是 TQue 的 `depth`（队列深度，进入队列的 IR 类型、对应 Ascend C 模板参数）；后者是 `init_buffer` 的 `num`（划分的内存块数，num=2 即开启 double buffer）。示例中两者都取 BUFFER_NUM=2，概念上却是两个独立参数。

**练习 2**：为什么 `deque` 要传 `dtype` 而 `alloc_tensor` 也要传？队列自己不记类型吗？

**答案**：不记。TQue 只管理「内存块 + 就绪状态」，`LocalTensor` 的类型信息由创建时的 `dtype` 参数带上 IR（`ir.get_local_tensor_type(dtype.to_ir())`），队列的 IR 类型里只有 pos 和 depth。所以 02 示例宁可在 `compute` 里多传一个 `z_gm` 也要拿到 dtype。

**练习 3**：`alloc_tensor` 的 non-inplace 与 in-place 两种重载，对 `depth` 的要求分别是什么？

**答案**：见 fwk/utils.py 中 alloc_tensor 的约束说明：non-inplace（`alloc_tensor(dtype)` 返回新张量）要求 depth 非零；in-place（`alloc_tensor(tensor)` 原地绑定）要求 depth 为 0。

### 4.3 TBuf 与 TQueBind：临时缓冲与跨位置数据通路

#### 4.3.1 概念说明

**TBuf**：算子中间需要一些「不过队列」的临时变量（如 reduce 的中间累加区、swiglu 的复用缓冲）。`TBuf` 只绑定一个逻辑位置、由 TPipe 划一整块内存，用 `get(dtype, len)` 从中取出张量。它没有 enque/deque 的队列语义，取出来的张量生命周期完全由你掌控——适合「一块内存反复重用」的场景（07_swiglu 示例正是这么干的）。

**TQueBind**：绑定「源逻辑位置 → 目的逻辑位置」两个位置的队列，是 TQue 的一般形式。`TQue(pos)` 相当于单位置的简化模式；当数据通路涉及特殊位置对（如 VECOUT→GM 直出）时直接用 `TQueBind(src, dst, depth)`。类文档（tpipe.py:24-27）原话：「通常情况下开发者使用 TQue 进行编程，TQueBind 对外提供一些特殊数据通路的内存管理和同步控制」。

注意一个容易混淆的点：pyasc 里 `TQue`/`TBuf` **继承自** `TQueBind`（Python 代码复用），这 是前端实现层面的父子关系；语义上三者对应 Ascend C 的三个不同类。后端甚至专门有一个 `AscendC_ToQueBindOp`（[TQue.td:146-153](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L146-L153)）用于把派生类实例转成 TQueBind 使用。

#### 4.3.2 核心流程

TBuf 的使用三步：

```text
buf = asc.TBuf(asc.TPosition.VECCALC)   # ① 声明缓冲（位置进入 IR 类型）
pipe.init_buffer(buf, byte_len)          # ② 划分 byte_len 字节（自动 32B 对齐补齐）
t  = buf.get(dtype, len)                 # ③ 取出张量；或 get_with_offset(size, off) 带偏移取
```

TQueBind 的构造与 TQue 的差别只在签名：

```text
asc.TQueBind(src=TPosition.VECIN, dst=TPosition.VECIN, depth=0, mask=0)
                  └── 两个位置可以不同，深度默认 0（配 in-place 接口）
asc.TQue(pos=TPosition.VECIN, depth=1)
                  └── 单位置，深度默认 1
```

#### 4.3.3 源码精读

**TBuf 构造的两段式**。[python/asc/language/fwk/tpipe.py:213-224](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L213-L224)：先用 `super().__init__(pos, pos, 0, 0)` 走一遍 TQueBind 构造，随后生成自己的 `asc.tbuf` 操作并把 handle 重新绑定，再以 handle 形式初始化父类。最终 IR 里留下的是 `AscendC_TBufOp`（[TBuf.td:25-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TBuf.td#L25-L29)），类型由 `builder.get_buffer_type(pos)` 给出。

```python
def __init__(self, pos=None, handle=None) -> None:
    if handle is not None:
        self.handle = handle
        return
    super().__init__(pos, pos, 0, 0)
    require_constexpr(pos, int, arg_name="pos")
    pos = ConstExpr.unwrap(pos)
    builder = global_builder.get_ir_builder()
    ir_type = builder.get_buffer_type(pos)
    self.handle = builder.create_asc_TBufOp(ir_type)
    super().__init__(handle=self.handle)
```

**get / get_with_offset**。[python/asc/language/fwk/tpipe.py:241-251](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L241-L251)：`get` 不传 `len` 时取整块缓冲，传 `len` 时取前 len 个元素，分别生成带/不带长度实参的 `asc.tbuf.get_tensor` 操作。[tpipe.py:257-267](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L257-L267) 的 `get_with_offset` 有一个**编译期就会触发**的校验：

```python
def get_with_offset(self, size: RuntimeInt, buf_offset: RuntimeInt, dtype: DataType) -> LocalTensor:
    if buf_offset % 32 != 0:
        raise ValueError("buf_offset must be align to 32B.")
    ...
```

`buf_offset` 不满足 32 字节对齐时，JIT 编译这行代码的瞬间就抛 `ValueError`——这是「前端把硬件约束前移到编译期」的典型例子（硬件要求 32B 对齐，Python 端替你提前拦住）。

**TQueBind 构造**。[python/asc/language/fwk/tpipe.py:39-53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L39-L53)：`src/dst/depth` 三个都要求 constexpr，进入 `get_quebind_type(src, dst, depth)` 类型后生成 `asc.que_bind` 操作（[TQue.td:191-195](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L191-L195)）。它带 `src_user_pos/dst_user_pos` 的 deque/enque 变体（tpipe.py:115-120、134-137）即服务于跨位置通路。

**TBufPool（了解即可）**。[python/asc/language/fwk/tpipe.py:285-363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L285-L363)：当多个 stage 计算导致 UB/L1 物理内存不足时，用 `TBufPool` 手动划分、复用内存池（`init_buf_pool` 划池、`init_buffer` 从池里分、`reset` 切池）。初学阶段用不到，遇到「UB 放不下」时再回来看。

#### 4.3.4 代码实践

**实践目标**：验证 TBuf 的 32 字节对齐校验与 get 的两种形态。

**操作步骤**：

1. 新建 `my_tbuf_probe.py`（示例代码）：

   ```python
   import asc
   from asc.runtime import config

   config.set_platform(config.Backend.Model, check=False)

   @asc.jit
   def tbuf_kernel():
       pipe = asc.TPipe()
       calc_buf = asc.TBuf(asc.TPosition.VECCALC)
       pipe.init_buffer(calc_buf, 1024)
       t_all = calc_buf.get(asc.int32)          # 取整块
       t_part = calc_buf.get(asc.int32, 128)    # 取 128 个元素

   tbuf_kernel[1]()
   ```

2. 运行一次确认通过；再增加一行 `t_off = calc_buf.get_with_offset(128, 33, asc.int32)`（偏移 33 字节，故意不对齐），重新运行。

**需要观察的现象**：第二次运行在 JIT 编译期立刻抛出 `ValueError: buf_offset must be align to 32B.`，根本不会走到设备执行；改成 64 后恢复正常。

**预期结果**：ValueError 由 [tpipe.py:260-261](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L260-L261) 的检查触发，这是纯 Python 分支，行为确定；tbuf_kernel 首次运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：什么场景选 TBuf 而不是 TQue？

**答案**：数据不需要「生产者→消费者」的队列语义、只是一块反复重用的临时工作区（如中间结果暂存、tiling 中间量）时用 TBuf；需要在流水线阶段间传递数据并依赖框架自动同步时用 TQue。

**练习 2**：TQueBind 相对 TQue 多表达了一个什么信息？它的 enque/deque 为什么要有带 `src_user_pos/dst_user_pos` 的变体？

**答案**：多表达了「数据从哪个逻辑位置流向哪个逻辑位置」（src→dst）。跨位置通路上入队/出队涉及的两个端点位置不同，需要显式告诉框架两端位置（生成 `que_bind.enque_tensor_pos` / `que_bind.deque_tensor_pos` 操作），单位置 TQue 用默认变体即可。

**练习 3**：`TBuf.get(asc.int32, 128)` 从一块 init_buffer 了 1024 字节的缓冲里取了多少字节？

**答案**：\(128 \times 4 = 512\) 字节。约束是 \( \text{len} \times \text{dtype.sizeof()} \le \text{init\_buffer 长度} \)（fwk/utils.py 中 get 的约束说明）。

### 4.4 两种编程风格对比：手动同步 vs 框架同步

#### 4.4.1 概念说明

同一个向量加法算子，仓库提供了两份等价实现，它们是理解本讲价值的最好教材：

- **01_add（手动风格）**：自己用 `LocalTensor(dtype, pos, addr, len)` 在 UB 上排布三块缓冲并手工计算字节偏移；自己按 `buf_id = i % BUFFER_NUM` 轮转双缓冲；自己在每步之间写三对 `set_flag/wait_flag`。
- **02_add_framework（框架风格）**：声明一个 TPipe 和三个 TQue；`init_buffer` 划内存；业务按 `copy_in / compute / copy_out` 三个 Device 函数组织，各自只用 `alloc/enque/deque/free` 表达数据流，**通篇没有一条同步指令**。

框架风格把「内存分配」和「同步插入」两件最容易出错的事都接管了：前者由 TPipe 的账本管理，后者由 EnQue/DeQue 在生成的 Ascend C 代码内部完成。

#### 4.4.2 核心流程

两个示例每轮迭代的指令流对照（均在 Kernel 内、双缓冲 BUFFER_NUM=2）：

```text
01_add 手动风格（add.py:49-69）            02_add 框架风格（add_framework.py:45-48）
──────────────────────────────            ──────────────────────────────
buf_id = i % BUFFER_NUM                    copy_in:  x = in_x.alloc_tensor(dtype)
data_copy(x_local[buf_id*tile:], ...)                data_copy(x, x_gm[...], tile)
data_copy(y_local[buf_id*tile:], ...)                in_x.enque(x)   （同步内置）
set_flag(MTE2_V, buf_id)                  compute:  x = in_x.deque(dtype)
wait_flag(MTE2_V, buf_id)                           z = out_z.alloc_tensor(dtype)
asc.add(z_local[...], x_local[...], ...)             asc.add(z, x, y, tile)
set_flag(V_MTE3, buf_id)                            out_z.enque(z)  （同步内置）
wait_flag(V_MTE3, buf_id)                           in_x.free_tensor(x)
data_copy(z_gm[...], z_local[...])        copy_out: z = out_z.deque(dtype)
set_flag(MTE3_MTE2, buf_id)                         data_copy(z_gm[...], z, tile)
wait_flag(MTE3_MTE2, buf_id)                        out_z.free_tensor(z)
```

内存排布对照：01_add 手工算 `buffer_size = tile_length * BUFFER_NUM * sizeof`，三个张量的 addr 分别取 0、buffer_size、2×buffer_size（[add.py:42-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L42-L47)）；02_add 只需三条 `init_buffer(que, BUFFER_NUM, tile_length * sizeof)`（[add_framework.py:42-44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L42-L44)），块划分与互斥使用全部交给框架。

#### 4.4.3 源码精读

**02 的 Kernel 主体**。[examples/02_add_framework/add_framework.py:28-48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L28-L48)：

```python
@asc.jit
def vadd_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress, block_length: int,
                tile_length: asc.ConstExpr[int]):
    offset = asc.get_block_idx() * block_length
    ...
    pipe = asc.TPipe()
    in_queue_x = asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)
    in_queue_y = asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)
    out_queue_z = asc.TQue(asc.TPosition.VECOUT, BUFFER_NUM)
    pipe.init_buffer(in_queue_x, BUFFER_NUM, tile_length * x.dtype.sizeof())
    ...
    for i in range(TILE_NUM * BUFFER_NUM):
        copy_in(i, x_gm, y_gm, in_queue_x, in_queue_y, tile_length)
        compute(z_gm, in_queue_x, in_queue_y, out_queue_z, tile_length)
        copy_out(i, z_gm, out_queue_z, tile_length)
```

注意两点：`tile_length` 标注为 `asc.ConstExpr[int]`（u2-l1），因为 TQue 的 depth 与 init_buffer 的划分要在编译期定死；三个 Device 子函数通过**参数**接收 TQue 对象——TQue 实现了 `IRValue` 协议（from_ir/to_ir），所以能像普通值一样在子函数间传递并在 IR 层对接（子函数内联机制见 u4-l4）。

**三个 Device 函数恰好演示了生命周期的生产/消费两端**。[add_framework.py:51-59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L51-L59) 的 `copy_in` 是生产者（alloc→data_copy→enque）；[add_framework.py:62-72](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L62-L72) 的 `compute` 是中间消费者兼生产者（deque→add→enque→free）；[add_framework.py:75-79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L75-L79) 的 `copy_out` 是终点消费者（deque→data_copy→free）。

**01 的手动同步对照**。[examples/01_add/add.py:44-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L44-L47) 手工排布三块 UB 内存（addr 依次为 0、buffer_size、2×buffer_size）；[add.py:49-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49-L69) 循环体内三对 `set_flag/wait_flag`（MTE2_V、V_MTE3、MTE3_MTE2）的含义已在 u2-l4 精读，此处只作对照：这些代码在 02 中**一条都不存在**。

**框架之外还有第三种风格：惰性张量 + 编译器插同步**。pyasc 还支持 `asc.LocalTensorAuto`（惰性创建张量，见 [python/asc/language/core/tensor.py:453](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L453)）风格。编译器通过 [python/src/IR.cpp:570-574](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L570-L574) 的 `need_insert_sync` 检测模块里是否出现 `LocalTensorAutoOp`：

```cpp
"need_insert_sync",
[](ModuleOp& self) {
    auto result = self.walk([](ascendc::LocalTensorAutoOp) { return WalkResult::interrupt(); });
    return result.wasInterrupted();
})
```

一旦检测到，[python/asc/runtime/compiler.py:180-181](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L180-L181) 把 `insert_sync` 选项默认置真，随后 [compiler.py:137-142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L137-L142) 依次跑 `erase_sync → hoist_que_bind → insert_sync → unify_pipe` 四个 Pass 重建同步。其中 `HoistQueBind` 会把 `QueueOp`、`TPipeInitQueueOp`、`TBufGetTensorOp` 等操作外提到循环外（[lib/Dialect/Asc/Transforms/HoistQueBind.cpp:29-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/HoistQueBind.cpp#L29-L46)）；`InsertSync` 会在每个写目标的算子后自动补 `enque` 或插入屏障（[lib/Dialect/Asc/Transforms/InsertSync.cpp:33-59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L33-L59)）。这条 Pass 链的完整精读放在 u6-l3，本讲只需建立印象：**同步可以由三重机制承接——手动 flag、队列框架、编译器 Pass**。

#### 4.4.4 代码实践

**实践目标**：拿到两种风格各自的 `ascendc.cpp`，直观对比同步指令差异。

**操作步骤**：

```bash
cd examples/01_add       && PYASC_DUMP_PATH=/tmp/dump01 python3 add.py -r Model
cd ../02_add_framework   && PYASC_DUMP_PATH=/tmp/dump02 python3 add_framework.py -r Model
grep -n "SetFlag\|WaitFlag\|EnQue\|DeQue\|AllocTensor\|FreeTensor" /tmp/dump01/**/*.ascendc.cpp
grep -n "SetFlag\|WaitFlag\|EnQue\|DeQue\|AllocTensor\|FreeTensor" /tmp/dump02/**/*.ascendc.cpp
```

**需要观察的现象**：

- dump01 的 ascendc.cpp：能在用户代码层面看到 `SetFlag<HardEvent::MTE2_V>` / `WaitFlag<...>` 成对出现（发射自 01 的手动调用）。
- dump02 的 ascendc.cpp：看到 `AllocTensor/EnQue/DeQue/FreeTensor` 成组出现，而用户代码层面没有显式 SetFlag/WaitFlag。

**预期结果**：具体文本形态「待本地验证」（取决于 dump 目录结构与发射格式），但「01 有显式同步指令、02 只有队列操作」这一对比关系由源码结构保证。

#### 4.4.5 小练习与答案

**练习 1**：02 示例一处同步都没写，为什么结果仍然正确？

**答案**：同步语义被 `enque/deque` 承接——它们发射为 Ascend C 的 `EnQue/DeQue` 成员函数调用，队列框架在类库内部处理就绪通知与等待；加上 `alloc/free` 保证内存块互斥，三对手动 flag 的职责被完整替代。

**练习 2**：把 02 的 `BUFFER_NUM` 改成 1 会发生什么？01 里能做同样修改吗？

**答案**：02 改成 1 后队列退化为单缓冲，搬运与计算无法重叠，功能仍正确但性能下降；需要相应保证 `TILE_NUM * BUFFER_NUM` 等切分关系成立（Host 侧 `tile_length = block_length // TILE_NUM // BUFFER_NUM`）。01 同样可改，但手动风格的 `buffer_size`、三块缓冲的 addr 排布都要联动重算——这正是框架风格的维护性优势。

**练习 3**：手动风格、框架风格、LocalTensorAuto 惰性风格三者的同步分别由谁负责？

**答案**：手动风格由开发者（set_flag/wait_flag + TPipe.alloc_event_id）；框架风格由 Ascend C 队列类库（EnQue/DeQue 内部）；惰性风格由 pyasc 编译器 Pass 链（`need_insert_sync` 检测 → EraseSync/HoistQueBind/InsertSync/UnifyPipe 自动重建）。

## 5. 综合实践

**任务**：把 `examples/01_add/add.py` 的手动 LocalTensor + set_flag 风格，改写成 02_add_framework 的 TPipe/TQue 风格，运行验证结果一致，并对比两份 dump 出的 ascendc.cpp 中同步指令的差异。

**步骤**：

1. **复制起点**：`cp examples/01_add/add.py my_add_framework.py`，基于它改写（这样保留 Host 侧启动代码）。
2. **改写 Kernel**：参考实现如下（示例代码，改写自 01/02 两个示例）：

   ```python
   @asc.jit
   def vadd_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress, block_length: int,
                   tile_length: asc.ConstExpr[int]):
       offset = asc.get_block_idx() * block_length
       x_gm = asc.GlobalTensor()
       y_gm = asc.GlobalTensor()
       z_gm = asc.GlobalTensor()
       x_gm.set_global_buffer(x + offset)
       y_gm.set_global_buffer(y + offset)
       z_gm.set_global_buffer(z + offset)

       pipe = asc.TPipe()                                    # ① 唯一的 TPipe
       in_queue_x = asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)
       in_queue_y = asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)
       out_queue_z = asc.TQue(asc.TPosition.VECOUT, BUFFER_NUM)
       pipe.init_buffer(in_queue_x, BUFFER_NUM, tile_length * x.dtype.sizeof())            # ② 划内存
       pipe.init_buffer(in_queue_y, BUFFER_NUM, tile_length * y.dtype.sizeof())
       pipe.init_buffer(out_queue_z, BUFFER_NUM, tile_length * z.dtype.sizeof())

       for i in range(TILE_NUM * BUFFER_NUM):
           x_local = in_queue_x.alloc_tensor(x_gm.dtype)     # ③ 生产者：alloc
           y_local = in_queue_y.alloc_tensor(y_gm.dtype)
           asc.data_copy(x_local, x_gm[i * tile_length:], tile_length)
           asc.data_copy(y_local, y_gm[i * tile_length:], tile_length)
           in_queue_x.enque(x_local)                         # ④ 入队（同步内置）
           in_queue_y.enque(y_local)

           x_local = in_queue_x.deque(x_gm.dtype)            # ⑤ 消费者：deque
           y_local = in_queue_y.deque(y_gm.dtype)
           z_local = out_queue_z.alloc_tensor(z_gm.dtype)
           asc.add(z_local, x_local, y_local, tile_length)
           out_queue_z.enque(z_local)
           in_queue_x.free_tensor(x_local)                   # ⑥ 归还内存块
           in_queue_y.free_tensor(y_local)

           z_local = out_queue_z.deque(z_gm.dtype)
           asc.data_copy(z_gm[i * tile_length:], z_local, tile_length)
           out_queue_z.free_tensor(z_local)
   ```

   > 提示：`out_queue_z` 的位置必须是 `VECOUT`（输出搬出队列），抄写成 `VECCALC` 之类会把数据通路接错——手动排布时代常见的错误，在框架风格里表现为「枚举参数选错」，同样要小心。
3. **改 Host 侧**：`tile_length` 现在是 `ConstExpr`，需在 launch 里显式算好传入（照抄 [add_framework.py:82-88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L82-L88)）：

   ```python
   def vadd_launch(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
       z = torch.zeros_like(x)
       total_length = z.numel()
       block_length = (total_length + USE_CORE_NUM - 1) // USE_CORE_NUM
       tile_length = block_length // TILE_NUM // BUFFER_NUM
       vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length)
       return z
   ```

4. **运行验证**：`python3 my_add_framework.py -r Model`，期望日志输出 run success 且 `torch.allclose` 断言通过。
5. **对比 dump**：分别在 01 与你的新文件上设置 `PYASC_DUMP_PATH` 运行，然后：

   ```bash
   grep -cE "SetFlag|WaitFlag"  <dump01>/...ascendc.cpp   # 预期：多条（3 对 × 循环）
   grep -cE "EnQue|DeQue"       <dump02>/...ascendc.cpp   # 预期：多条
   grep -cE "SetFlag|WaitFlag"  <dump02>/...ascendc.cpp   # 预期：0 或仅在类库内联处出现
   ```

**检查清单**：改写后你的文件里应当——没有任何 `asc.LocalTensor(...)` 四参构造、没有 `buffer_size` 手工偏移、没有 `set_flag/wait_flag`；取而代之的是一个 TPipe、三个 TQue、三次 init_buffer 和成组的 alloc/enque/deque/free。运行结果「待本地验证」。

## 6. 本讲小结

- **TPipe 是总管**：一个 Kernel 全局唯一（`TPipeManager` 在编译期强制检查并在 teardown 自动复位），`init_buffer` 的两种重载分别为 TQue（num 块 × len 字节）与 TBuf（len 字节）划分内存，`alloc/fetch/release_event_id` 服务于混合编程中的手动同步。
- **TQue 用四步生命周期编程**：`alloc_tensor`（要内存）→ `enque`（宣布就绪）→ `deque`（取就绪数据）→ `free_tensor`（归还内存块）；顺序承载正确性，dtype 需显式传入因为队列不记类型。
- **TBuf 管临时缓冲、TQueBind 表达跨位置通路**：TBuf 的 `get/get_with_offset`（32 字节对齐在编译期校验）适合反复重用的工作区；TQueBind(src, dst, depth) 是 TQue 的一般形式，Special 通路用它。
- **同步有三重承接机制**：手动 `set_flag/wait_flag`（01 风格）、队列框架 EnQue/DeQue 内置（02 风格）、`need_insert_sync` 检测触发的 EraseSync→HoistQueBind→InsertSync→UnifyPipe Pass 链（LocalTensorAuto 惰性风格，u6-l3 精读）。
- **所有接口一一镜像 Ascend C**：Python 方法名 → IR 助记符（如 `enque` → `asc.que_bind.enque_tensor`）→ C++ 成员函数（`EnQue`），权威参数与约束说明在 fwk/utils.py 的文档表中，可通过 `set_tpipe_docstring` 注入的 docstring 直接 `help()` 查看。

## 7. 下一步学习建议

本讲完成了 language 层用户接口的全部基础内容。接下来：

1. **进入第 3 单元（runtime 模块）**：从 u3-l1「@asc.jit 装饰器」开始，沿 JIT 执行顺序弄清这些前端对象是在哪个时机、被谁驱动着生成 IR 的——本讲反复出现的 `global_builder` 的生命周期会在 u5-l6 详细展开。
2. **提前预習 u4-l4（Device 子函数内联）**：02 示例的 copy_in/compute/copy_out 三段式是最好的素材，理解 TQue 对象如何作为参数在子函数间传递并内联进同一个 IR 模块。
3. **源码阅读建议**：通读 [python/asc/language/fwk/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/utils.py) 的文档表（对照 `help(asc.TQue.deque)`），再浏览 [examples/07_swiglu/swiglu.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/07_swiglu/swiglu.py) 看 TBuf 复用与双路 VECIN 的实战用法，为第 7 单元的高阶 API 打底。
