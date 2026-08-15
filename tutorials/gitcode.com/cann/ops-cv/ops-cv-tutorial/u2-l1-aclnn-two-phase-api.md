# aclnn 两段式接口与基础数据结构

## 1. 本讲目标

上一讲（u1-l4）我们用 AddExample 打通了「编译 → 安装 → 运行」闭环，但当时把样例代码当成黑盒。本讲拆开这个黑盒，学完后你应该能：

1. 说出 `aclnnXxxGetWorkspaceSize` 与 `aclnnXxx` 两段式接口各自的职责与分工。
2. 理解 `aclTensor`、`aclOpExecutor`、`workspace` 三个核心概念在算子调用中扮演的角色。
3. 独立读懂任何一个算子仓库 `examples/` 目录下的 aclnn 调用 main 函数，并能照着写出自己的最小调用程序。

## 2. 前置知识

### 2.1 Host 侧与 Device 侧

- **Host**：指 CPU 侧，负责准备数据、发起任务、管理资源。
- **Device**：指 NPU 卡侧，真正的计算发生在 AI Core / AI CPU 上。
- Host 与 Device 的内存是分离的，数据要计算必须先用 `aclrtMemcpy` 从 Host 拷到 Device，算完后还要拷回来。

### 2.2 同步执行与异步执行

- `aclrtMemcpy` 这类带方向的拷贝是同步的：调用返回时数据已经就位。
- 算子执行接口是**异步下发**的：调用返回只代表「任务已提交到 stream（任务流）」，不代表算子已经算完。所以样例里总会看到 `aclrtSynchronizeStream(stream)`——它的语义是「阻塞等待这条流上的所有任务执行完毕」。

### 2.3 aclTensor 是什么

`aclTensor` 是 CANN 对「张量」的统一描述结构，由 `aclCreateTensor` 创建，包含：shape（各维长度）、strides（各维步长，用于描述内存布局是否连续）、dataType（如 `ACL_FLOAT`）、format（如 `ACL_FORMAT_ND` / `ACL_FORMAT_NCHW`）以及一块 device 侧内存地址。可以把它类比成 PyTorch 的 `Tensor`，但它只是一个「描述符 + 指针」，本身不拥有数据。

### 2.4 本讲会遇到的术语速查

| 术语 | 含义 |
| --- | --- |
| `aclnnStatus` | aclnn 接口的返回状态码，`ACLNN_SUCCESS`(0) 表示成功 |
| `aclrtStream` | 任务流，异步任务的提交通道 |
| `workspace` | 算子在 NPU 上完成计算所需的**临时内存**（输入输出之外） |
| `aclOpExecutor` | 算子执行器，由框架在第一段接口内生成，封装了整个计算流程 |
| `aclFloatArray` | float 数组类型的属性参数（如 Resize 的 scales），对应还有 `aclIntArray` 等 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/context/two_phase_api.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/context/two_phase_api.md) | 官方文档：两段式接口的调用规范与注意事项 |
| [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp) | AddExample 的 aclnn 单算子调用样例（教学算子，最简洁） |
| [image/resize_bilinear_v2/examples/test_aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp) | aclnnResize 真实算子调用样例（本讲实践的基础） |
| [image/resize_bilinear_v2/op_api/aclnn_resize.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.h) | aclnnResize 两段式接口的声明与参数注释 |
| [common/inc/op_api/aclnn_check.h](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/aclnn_check.h) | op_api 公共头文件示例：芯片架构判断工具 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **两段式接口规范**：为什么拆成两段、每段做什么、有哪些禁忌。
2. **aclTensor 等基础数据结构的构造**：样例里 CreateAclTensor / Init 这些「固定写法」逐行看懂。
3. **op_api 公共头文件初识**：以 aclnn_check.h 为例了解 common/inc/op_api 公共层。

### 4.1 两段式接口规范

#### 4.1.1 概念说明

调用一个单算子 aclnn API（例如 `aclnnAddExample`、`aclnnResize`）时，并不是一次函数调用完成的，而是固定的**两段式**：

- **第一段 `aclnnXxxGetWorkspaceSize`**：做参数校验、推导计算流程，回答两个问题——「这次计算需要多大的临时内存（workspaceSize）」以及生成一个 `aclOpExecutor`（执行器）。它**不执行计算**。
- **第二段 `aclnnXxx`**：拿着第一段给出的 workspace 地址和执行器，把计算任务异步下发到指定的 stream 上。

为什么要这么设计？直观理解：算子在不同 shape、不同数据类型下需要的临时空间差异很大，框架必须先「算一遍需要多少内存」，才能让用户（或内存池）按需申请；同时执行器把校验、tiling 选择等重活儿在第一段一次性做完，第二段就可以非常轻量地重复喂给硬件流水线。

#### 4.1.2 核心流程

```text
用户代码                                CANN 框架
   │
   ├── ① 构造输入/输出 aclTensor ──────► device 内存 + 描述符
   │
   ├── ② aclnnXxxGetWorkspaceSize(...) ─► 参数校验 / 生成执行计划
   │        │                             返回 workspaceSize + executor
   │        ▼
   ├── ③ 按 workspaceSize 申请 device 内存（可为 0，则跳过）
   │
   ├── ④ aclnnXxx(workspace, size, executor, stream) ─► 任务异步下发
   │
   ├── ⑤ aclrtSynchronizeStream(stream) ─► 等待算子算完
   │
   └── ⑥ aclrtMemcpy 拷回结果 → 释放资源
```

两个重要约束（来自官方文档）：

1. **必须先调第一段**，拿到 workspaceSize 并申请内存后才能调第二段。
2. **第二段接口不能对同一个 executor 重复调用**，重复调用会出现异常。要再算一次，就得重新走一遍两段式。

#### 4.1.3 源码精读

官方规范只有短短二十几行，但句句都是约束，值得全文细读：

[docs/zh/context/two_phase_api.md:L3-L12](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/context/two_phase_api.md#L3-L12) —— 给出两段式接口的标准签名：第一段接收算子全部输入输出，输出 `workspaceSize` 与 `executor` 两个出参；第二段只接收 workspace 指针、大小、executor 和 stream 四个参数。

[docs/zh/context/two_phase_api.md:L14-L23](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/context/two_phase_api.md#L14-L23) —— 「说明」部分定义了 workspace 的含义（输入/输出之外算子完成计算所需的临时内存），并用反例明确第二段接口不可重复调用。

再看真实算子的接口声明，验证文档与实现一致。以 Resize 为例：

[image/resize_bilinear_v2/op_api/aclnn_resize.h:L32-L33](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.h#L32-L33) —— 第一段 `aclnnResizeGetWorkspaceSize` 的声明：输入 `self`（Tensor）、`scales`（aclFloatArray）、`mode`（字符串属性），输出 `out`（Tensor），出参 `workspaceSize` 和 `executor`。

[image/resize_bilinear_v2/op_api/aclnn_resize.h:L46-L46](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.h#L46) —— 第二段 `aclnnResize` 的声明：只接收 workspace、workspaceSize、executor、stream 四个参数，与文档给出的通用签名完全一致。任何一个 aclnn 算子的第二段签名都是这个样子，**可以当公式背下来**。

顺带注意 [aclnn_resize.h:L41-L43](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/op_api/aclnn_resize.h#L41-L43) 的参数注释：workspace 大小「由第一段接口获取」、executor「包含了算子计算流程」、stream 是任务流——这正是三个核心概念的官方一句话定义。

#### 4.1.4 代码实践

**实践目标**：不写任何新代码，仅通过「读 + 断点式观察」验证两段式的分工。

**操作步骤**：

1. 打开 [examples/add_example/examples/test_aclnn_add_example.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp)，定位到 L122 与 L133 两处调用。
2. 在两处调用之间（L125-L130）阅读 workspace 申请逻辑：`workspaceSize > 0` 才调用 `aclrtMalloc`。
3. 如果你在 u1-l4 已经跑通过样例，重新运行一次，在第一段调用后用 `printf` 打印 `workspaceSize`（参照 [image/resize_bilinear_v2/examples/test_aclnn_resize.cpp:L124](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp#L124) 的写法，Resize 样例自带这一行打印）。
4. 把第二段调用复制一份紧跟着再调用一次（仅做实验，验证后撤销修改，不要提交）。

**需要观察的现象**：

- AddExample 这类简单逐元素算子的 workspaceSize 通常为 0（不需要临时内存）。
- 重复调用第二段接口时程序行为异常（报错或结果错误），印证文档 L17-L23 的约束。

**预期结果**：直观体会「第一段轻量可反复、第二段不可重复」的接口契约。若无法在本地环境运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么第二段接口不再需要传输入 Tensor？

**参考答案**：输入输出的全部信息（shape、dtype、内存地址）都在第一段调用时被封装进了 `aclOpExecutor`，第二段只需要 workspace 和 executor 即可，框架从 executor 中取出计算流程和Tensor 信息下发执行。

**练习 2**：如果 `workspaceSize` 为 0，还需要调用 `aclrtMalloc` 吗？

**参考答案**：不需要。两个官方样例都写了 `if (workspaceSize > 0)` 的守卫（AddExample 在 [test_aclnn_add_example.cpp:L127-L130](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L127-L130)，Resize 在 [test_aclnn_resize.cpp:L121-L125](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp#L121-L125)），释放时同样有对应守卫，避免对空指针 malloc/free。

**练习 3**：`aclnnResize` 的第二段接口有 4 个参数，`aclnnAddExample` 的第二段也是 4 个参数，这是巧合吗？

**参考答案**：不是。两段式接口的第二段签名是全局统一的 `(void* workspace, uint64_t workspaceSize, aclOpExecutor* executor, aclrtStream stream)`，见 [docs/zh/context/two_phase_api.md:L6-L7](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/context/two_phase_api.md#L6-L7)。差异只体现在第一段的算子专属参数上。

### 4.2 aclTensor 构造与样例九步骨架

#### 4.2.1 概念说明

所有 aclnn 样例的 main 函数都长着同一张脸，u1-l4 已经总结过「九步骨架」，本讲把其中与数据结构相关的步骤拆开讲透：

1. `Init`：`aclInit` → `aclrtSetDevice` → `aclrtCreateStream`。
2. 构造输入/输出 `aclTensor`：申请 device 内存 → H2D 拷贝 → 算 strides → `aclCreateTensor`。
3. （属性参数构造，如 `aclCreateFloatArray`。）
4. 第一段接口 + 按 workspaceSize 申请 workspace。
5. 第二段接口下发计算。
6. `aclrtSynchronizeStream` 同步。
7. D2H 拷回并打印结果。
8. 释放 tensor、device 内存、stream。
9. `aclFinalize`。

其中 `CreateAclTensor` 是理解 `aclTensor` 的关键：它展示了「数据」与「描述符」的分离——先在 device 上准备好裸内存，再用 `aclCreateTensor` 生成一个指向这块内存的描述符。

#### 4.2.2 核心流程

`CreateAclTensor(hostData, shape, deviceAddr, dataType, tensor)` 的执行过程：

```text
1. size = ∏shape × sizeof(T)          # 计算字节数
2. aclrtMalloc(deviceAddr, size)       # device 侧申请内存
3. aclrtMemcpy(device ← host)          # 数据搬到 device
4. 从后往前算连续 strides：
   strides[last] = 1
   strides[i] = shape[i+1] × strides[i+1]
5. aclCreateTensor(shape, dataType, strides, offset=0,
                   format, storageShape, deviceAddr)  # 生成描述符
```

strides 的数学含义：第 \(i\) 维移动一个下标，地址需要跨过 \( \text{strides}[i] \) 个元素。对连续张量，\( \text{strides}[i] = \prod_{j>i} \text{shape}[j] \)。

#### 4.2.3 源码精读

以 AddExample 样例为基准走读：

[examples/add_example/examples/test_aclnn_add_example.cpp:L51-L61](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L51-L61) —— `Init` 函数，注释直接写着「固定写法」：`aclInit` 初始化 ACL 框架、`aclrtSetDevice` 绑定设备、`aclrtCreateStream` 创建任务流。任何 aclnn 样例开头都是这三步。

[examples/add_example/examples/test_aclnn_add_example.cpp:L63-L85](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L63-L85) —— `CreateAclTensor` 模板函数：申请 device 内存（L69）、H2D 拷贝（L72）、计算连续 strides（L76-L79）、调用 `aclCreateTensor` 生成描述符（L82-L83）。注意 `aclCreateTensor` 传了两次 shape：第一次是逻辑 shape（含 strides），第二次是 storageShape（物理存储形状），format 用 `ACL_FORMAT_ND`。

[examples/add_example/examples/test_aclnn_add_example.cpp:L96-L115](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L96-L115) —— main 中构造两个输入 `selfX`/`selfY` 和一个输出 `out`，shape 均为 `{32,4,4,4}`，dtype 均为 `ACL_FLOAT`。**输出 tensor 也要先分配内存**——aclnn 的输出是「预分配」的，框架不会替你 malloc 输出空间。

[examples/add_example/examples/test_aclnn_add_example.cpp:L118-L134](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L118-L134) —— 两段式调用本体：L122 第一段拿到 `workspaceSize` 和 `executor`；L127-L130 条件申请 workspace；L133 第二段把任务发到 stream。

[examples/add_example/examples/test_aclnn_add_example.cpp:L136-L146](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L136-L146) —— 同步（L137）后 `PrintOutResult` 用 `ACL_MEMCPY_DEVICE_TO_HOST` 把结果拷回 host 打印。同步这一步绝不能省，否则拷回的可能是还没算完的旧数据。

[examples/add_example/examples/test_aclnn_add_example.cpp:L143-L159](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/examples/test_aclnn_add_example.cpp#L143-L159) —— 清理段：`aclDestroyTensor` 释放描述符、`aclrtFree` 释放 device 内存（workspace 也有条件释放）、销毁 stream、`aclrtResetDevice` + `aclFinalize`。注意「描述符」和「内存」是分别释放的两类资源。

再看真实算子 Resize 的样例，重点看它与教学样例的差异点：

[image/resize_bilinear_v2/examples/test_aclnn_resize.cpp:L87-L98](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp#L87-L98) —— 输入 shape `{1,1,2,2}`、输出 shape `{1,1,4,4}`：**输出 shape 由用户按算子语义自己算好并分配**，scales `{1.0,1.0,2.0,2.0}` 表示 H、W 各放大 2 倍。

[image/resize_bilinear_v2/examples/test_aclnn_resize.cpp:L108-L110](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp#L108-L110) —— 用 `aclCreateFloatArray` 构造 scales 属性参数，销毁时用配对的 `aclDestroyFloatArray`（L145）。这展示了「属性参数也有自己的创建/销毁对」。

[image/resize_bilinear_v2/examples/test_aclnn_resize.cpp:L115-L128](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp#L115-L128) —— `mode = "nearest"` 作为字符串属性传入第一段（L117），两段式骨架与 AddExample 完全一致；L124 额外打印了 workspaceSize，方便观察真实算子是否需要临时内存。

#### 4.2.4 代码实践

**实践目标**：编写一个基于 `aclnnResize` 的最小调用程序：构造 `1x1x4x4` 的 float 输入，用 scales `{1,1,2,2}`、mode `"nearest"` 放大到 `1x1x8x8`，与 CPU 参考实现逐元素对比。

**操作步骤**：

1. 复制 [image/resize_bilinear_v2/examples/test_aclnn_resize.cpp](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/image/resize_bilinear_v2/examples/test_aclnn_resize.cpp) 到同目录一个新文件（如 `test_aclnn_resize_my.cpp`，示例代码，不要提交），只改 main 中间这一段：

```cpp
// 示例代码：仅修改 main 中的输入构造部分，其余骨架保持不变
std::vector<int64_t> selfShape = {1, 1, 4, 4};
std::vector<int64_t> outShape  = {1, 1, 8, 8};   // 4x4 经 scales 2x 放大

// 4x4 输入：每行递增，便于肉眼核对 nearest 复制效果
std::vector<float> selfHostData = {
    1.0f,  2.0f,  3.0f,  4.0f,
    5.0f,  6.0f,  7.0f,  8.0f,
    9.0f, 10.0f, 11.0f, 12.0f,
   13.0f, 14.0f, 15.0f, 16.0f};
std::vector<float> scalesData = {1.0f, 1.0f, 2.0f, 2.0f};
std::vector<float> outHostData(64);

const char* mode = "nearest";  // nearest 最容易手算参考结果
```

2. 两段式调用保持原样（L117 的 `aclnnResizeGetWorkspaceSize` 与 L127 的 `aclnnResize`）。
3. 结果拷回后，增加与 CPU 参考实现的对比循环：

```cpp
// 示例代码：nearest 2 倍上采样的 CPU 参考实现与逐元素比对
int H = 4, W = 4;
for (int i = 0; i < 8; i++) {
    for (int j = 0; j < 8; j++) {
        float expect = selfHostData[(i / 2) * W + (j / 2)];
        float actual = resultData[i * 8 + j];
        if (std::fabs(expect - actual) > 1e-6) {
            printf("MISMATCH at (%d,%d): expect %f, got %f\n", i, j, expect, actual);
        }
    }
}
printf("compare done\n");
```

4. 参照 u1-l4 的方式编译运行（Resize 算子包需用 `--ops resize_bilinear_v2` 编译安装，运行时链接其 op_api 库）。

**需要观察的现象**：

- 第一段调用后打印的 `workspaceSize` 值（resize 类算子可能非 0）。
- 8x8 输出中每个 2x2 小块内的 4 个值相同，都等于输入对应位置的值。
- 对比循环输出 `compare done` 且没有任何 `MISMATCH` 行。

**预期结果**：NPU 输出与 CPU 最近邻参考实现完全一致。若本地暂无配套环境，请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把输入 shape 改成 `{2,3,4,4}` 但 host 数据仍是 16 个元素，会发生什么？

**参考答案**：`CreateAclTensor` 中 `size = ∏shape × sizeof(T)` 会按 96 个 float 申请内存并拷贝，而 `hostData.data()` 只有 16 个 float，`aclrtMemcpy` 会越界读 host 内存，行为未定义。构造时必须保证 host 数据长度与 shape 各维乘积一致。

**练习 2**：为什么 `aclrtSynchronizeStream` 不能省略？

**参考答案**：第二段接口只是把任务异步提交到 stream 就返回了，此时输出 device 内存中的结果还未就绪；不同步就直接 D2H 拷贝，拷到的可能是旧值/垃圾值。

**练习 3**：`aclCreateTensor` 的 strides 参数有什么用？什么时候会出现非连续 strides？

**参考答案**：strides 描述各维下标与内存地址的映射关系，让框架能正确读写非连续内存。例如对一个大 tensor 做切片（`x[:, :, ::2]`）后，末维 stride 就是 2。样例中都是新造的连续 tensor，所以按公式从后往前乘出来；u2-l2 会看到 op_api 实现里专门有对「非连续输入」的处理分支。

### 4.3 op_api 公共头文件初识：aclnn_check.h

#### 4.3.1 概念说明

上一讲提过 `common/` 目录是全仓库共享的公共代码层。本讲只初识其中一个很小的头文件 `aclnn_check.h`，它回答一个真实问题：**op_api 实现里如何判断当前跑在哪代芯片架构上？** 后续讲义（u3-l6）会系统走读 common 目录，这里先建立「算子 op_api 代码会依赖 common 公共能力」的印象。

#### 4.3.2 核心流程

```text
op_api 实现 → 调用 op::IsRegBase()
                → GetCurrentPlatformInfo().GetCurNpuarch()
                → 判断是否属于 {DAV_3510} 架构集合
                → 返回 true/false，走不同分支
```

#### 4.3.3 源码精读

[common/inc/op_api/aclnn_check.h:L23-L28](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/aclnn_check.h#L23-L28) —— `IsRegBase()` 无参版本：取当前平台 NPU 架构，判断是否落在 `regbaseArch` 集合（目前只有 `NpuArch::DAV_3510`）。`const static` 保证集合只构造一次。

[common/inc/op_api/aclnn_check.h:L30-L34](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/common/inc/op_api/aclnn_check.h#L30-L34) —— 带参重载版本：判断**指定架构**（例如 infershape 阶段拿到候选架构列表时）是否属于该集合，两个重载共用同一份架构集合定义。

#### 4.3.4 代码实践

**实践目标**：体会「公共头文件被哪些算子 op_api 引用」。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "aclnn_check.h" --include=*.cpp image/ objdetect/ | head -20`（源码阅读型实践，不修改任何文件）。
2. 挑一个引用了它的 op_api 实现文件，观察 `IsRegBase()` 出现的上下文：它在什么参数组合下被调用、返回 true/false 分别走什么逻辑。

**需要观察的现象**：引用该头文件的算子数量，以及调用点通常出现在参数校验/分支判断处。

**预期结果**：能看到多个 image 类算子的 aclnn 实现引用此头文件，调用点用于按架构区分行为。具体引用清单「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么把架构集合声明为函数内的 `const static std::set`？

**参考答案**：`static` 保证局部变量只初始化一次（首次调用时构造集合），后续调用零构造开销；`const` 防止误改。这是 C++ 中「函数内缓存常量表」的惯用法。

**练习 2**：`IsRegBase()` 与 `IsRegBase(NpuArch arch)` 两个重载分别适用什么场景？

**参考答案**：无参版在**运行时**查询当前实际芯片架构，用于第二段执行前的分支判断；带参版用于判断某个**候选/声明架构**（如 infershape 阶段对入参描述的架构做校验），不需要真的跑在那种芯片上。

## 5. 综合实践

把本讲三个模块串起来：**写一份「aclnn 样例阅读笔记」模板，并用它解析一个新算子**。

1. 从仓库 `image/` 或 `objdetect/` 下任选一个带 `examples/` 目录的算子（如 `grid_sample`）。
2. 用本讲的九步骨架给它的 aclnn 样例逐段标注：Init、Tensor 构造、属性构造、第一段、workspace 申请、第二段、同步、拷回、清理。
3. 找到该算子 op_api 目录下的头文件，抄下两段式接口签名，与 `docs/zh/context/two_phase_api.md` 的通用签名逐参数对照，指出算子专属参数有哪些。
4. 完成 4.2.4 的 aclnnResize 最小调用程序并跑通比对。

完成后你应当具备一种能力：拿到任何一个 CANN 算子仓库，不看文档也能在 5 分钟内写出它的最小 aclnn 调用程序。

## 6. 本讲小结

- aclnn 单算子调用是**两段式**：第一段 `GetWorkspaceSize` 负责校验并产出 `workspaceSize` 与 `aclOpExecutor`，第二段负责把计算异步下发到 stream；第二段不可对同一 executor 重复调用。
- `workspace` 是输入输出之外算子在 NPU 上所需的临时内存，大小由第一段返回，为 0 时可跳过申请与释放。
- `aclTensor` 是「描述符 + device 指针」：先 malloc + H2D 拷贝准备数据，再用 `aclCreateTensor` 携带 shape/strides/dtype/format 生成描述符；输出空间由用户预分配。
- 样例九步骨架（Init → 构造 → 第一段 → workspace → 第二段 → 同步 → 拷回 → 清理 → Finalize）适用于所有算子，差异只在构造输入和属性参数部分。
- 属性参数有自己的创建/销毁对（如 `aclCreateFloatArray` / `aclDestroyFloatArray`），字符串属性直接传 `const char*`。
- `common/inc/op_api` 提供跨算子公共能力，`aclnn_check.h` 的 `IsRegBase()` 是芯片架构判断的一个具体例子。

## 7. 下一步学习建议

下一讲 **u2-l2（op_api 层源码走读：以 aclnnResize 为例）** 将从「调用者视角」切换到「实现者视角」：打开 `image/resize_bilinear_v2/op_api/aclnn_resize.cpp`，看第一段接口内部到底做了哪些参数校验、非连续 tensor 如何处理、`aclOpExecutor` 是怎么一步步被构造出来的。建议先自行浏览该文件，带着「GetWorkspaceSize 里每一行校验在防什么坑」的问题进入下一讲。
