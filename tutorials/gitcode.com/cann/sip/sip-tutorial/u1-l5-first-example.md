# 第一个算子调用：跑通 example

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立编译并运行 `example` 目录下的算子 Demo（asdBlasSdot 点积）。
2. 闭着眼睛说出 SiP 算子调用的固定套路：**ACL 初始化 → 创建 aclTensor → 创建 handle → MakePlan → 查询并申请 workspace → 绑定 stream → 执行 → 同步 → 销毁 → 拷回结果 → 释放资源**。
3. 知道 `example/A2` 子目录按「模块/算子」组织了每个算子的专属样例，并能自己找到任意算子的可运行示例。

本讲是入门单元的最后一讲：u1-l3 编译出了库，u1-l4 装好了环境，本讲终于让程序真正在 NPU 上算出第一个结果。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（均在前几讲出现过，这里用更通俗的语言再讲一遍）：

- **Host 与 Device**：Host 指 CPU 侧（你的程序本身），Device 指 NPU（昇腾芯片）。数据平时在 Host 的内存里，计算前必须搬到 Device 的显存里，算完再搬回来。
- **ACL（Ascend Computing Language）**：昇腾的计算加速层运行时库，是所有昇腾程序的最底层"操作系统接口"。`aclrtMalloc`（申请显存）、`aclrtMemcpy`（搬运数据）、`aclrtStream`（任务流）都来自它。SiP 是建立在 ACL 之上的信号处理加速库。
- **aclTensor**：ACL 提供的"张量描述符"。它本身不持有数据，只是把 shape（形状）、dtype（数据类型）、strides（步长）、显存地址等信息打包成一件事物，让算子接口能统一描述输入输出。
- **句柄（handle）**：SiP 的算子不直接用裸函数调用，而是先创建一个不透明的 `asdBlasHandle`（本质是 `void*`，见 [include/blas_api.h:L20](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L20)），后续所有操作都围绕这个句柄进行。这种设计与 cuBLAS 的 `cublasHandle_t` 一脉相承——好处是执行计划、workspace、stream 等重资源可以挂在句柄上反复复用。
- **stream（流）与同步**：向 NPU 下发任务是异步的——`asdBlasSdot` 返回时计算未必完成。任务被排入一个 `aclrtStream`，`asdBlasSynchronize` 会阻塞等待该流上的所有任务结束，之后才能读取结果。
- **ASDSIP_HOME_PATH**：u1-l4 讲过，安装目录下的 `set_env.sh` 会导出这个变量，指向 SiP 的安装根（latest 软链）。编译 Demo 时靠它找到头文件和库。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `example/example.cpp` | 主样例：计算两个 float 向量的点积（asdBlasSdot），完整演示 ACL 初始化与 SiP 固定调用套路 |
| `example/build.sh` | 主样例的一键编译运行脚本：检查环境变量、拼 g++ 命令、执行 |
| `example/README.md` | 官方使用说明：环境配置、SiP 编译、运行 demo 三步 |
| `include/blas_api.h` | BLAS 模块公开头文件，Demo 用到的 8 个 asdBlas 接口全部声明于此 |
| `example/CMakeLists.txt` | example 的另一种构建方式（CMake），与 build.sh 二选一 |
| `example/A2/` | 按模块组织的全部算子专属样例（本讲的"样例地图"） |

## 4. 核心概念与源码讲解

### 4.1 ACL 初始化：让程序"登上" NPU

#### 4.1.1 概念说明

任何昇腾程序在调用算子之前，都要先完成三件事：

1. **初始化 ACL 运行时**（`aclInit`）——加载驱动与运行时资源，全程只需一次。
2. **指定设备**（`aclrtSetDevice`）——告诉运行时用几号卡（机器可能插多张 NPU）。
3. **创建任务流**（`aclrtCreateStream`）——后续下发的计算任务都排到这条流上。

这三步与 SiP 无关，是所有 ACL 程序的通用前置动作，源码注释里也称之为"固定写法"。

此外，算子的输入输出不能是 C++ 的 `std::vector`，必须是 Device 侧的 `aclTensor`。创建一个 aclTensor 分四小步：申请显存 → 把 Host 数据拷进去 → 计算 strides → 调 `aclCreateTensor` 组装描述符。

#### 4.1.2 核心流程

```text
程序启动
  ├─ aclInit(nullptr)              # 初始化 ACL 运行时
  ├─ aclrtSetDevice(deviceId)      # 选定 0 号 NPU
  ├─ aclrtCreateStream(&stream)    # 创建任务流
  └─ （对每个输入/输出张量）
       ├─ aclrtMalloc              # 在 Device 显存申请空间
       ├─ aclrtMemcpy H2D          # Host 数据 → Device
       ├─ 按 shape 计算 strides    # 连续张量：strides[i] = shape[i+1] * strides[i+1]
       └─ aclCreateTensor          # 打包成 aclTensor
```

#### 4.1.3 源码精读

初始化三连，Demo 把它封装成了 `Init` 函数（[example/example.cpp:L28-L35](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L28-L35)）：`aclInit` → `aclrtSetDevice` → `aclrtCreateStream`，注释明确写着"固定写法，acl初始化"。

创建 aclTensor 的逻辑被封装成函数模板 `CreateAclTensor<T>`（[example/example.cpp:L37-L57](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L37-L57)），关键四行：

- [L43](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L43)：`aclrtMalloc` 以 `ACL_MEM_MALLOC_HUGE_FIRST` 优先申请大页显存（对 NPU 带宽更友好）。
- [L45](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L45)：`ACL_MEMCPY_HOST_TO_DEVICE` 方向的 `aclrtMemcpy` 完成数据搬运。
- [L48-L51](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L48-L51)：从倒数第二维往前累乘，得到连续张量的 strides。
- [L54-L55](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L54-L55)：`aclCreateTensor` 传入 shape、dataType、strides、format（`ACL_FORMAT_ND` 表示自然多维格式）与 Device 地址。

顺带一提：[example/example.cpp:L15](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L15) include 的 `acl_meta.h` 并不在 SiP 仓库里，它来自 CANN 安装目录的 aclnn 头文件（build.sh 中 `-I${ASCEND_HOME_PATH}/include/aclnn` 提供该路径）。

#### 4.1.4 代码实践

1. **实践目标**：搞清楚"一个 aclTensor 背后有几次显存操作"，为后面改代码建立直觉。
2. **操作步骤**：数一数 Demo 主函数里创建了多少个 aclTensor（提示：见 [example/example.cpp:L112-L114](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L112-L114)），并画出 x、y、result 各自的 Host buffer 与 Device 地址之间的拷贝方向箭头。
3. **需要观察的现象**：纸面上应有 3 次 `aclrtMalloc` + 3 次 H2D 拷贝 + 1 次 D2H 拷贝（结果拷回）。
4. **预期结果**：输入张量要 H2D，输出张量也要先 H2D（占位初值）后 D2H——输出张量同样走 `CreateAclTensor` 创建，初值无意义但空间必须先申请好。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `aclInit` 全进程只调用一次，而 `aclrtCreateStream` 可以多次调用？
**答案**：`aclInit` 加载的是进程级运行时资源（驱动、算子加载框架），重复初始化没有意义；stream 是逻辑上的任务队列，程序可以为不同用途（如多条并行流水线）创建多条流，因此可多次调用。

**练习 2**：如果 shape 为 `{2, 3, 4}`，按 [L48-L51](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L48-L51) 的算法，strides 是多少？
**答案**：从末尾往前：strides = {1,1,1} → strides[1] = 4×1 = 4 → strides[0] = 3×4 = 12，最终 strides = {12, 4, 1}，即沿各维度走一个元素分别跳 12、4、1 个底层元素。

### 4.2 Demo 主流程：Handle → Plan → Workspace → Stream → 执行 → 同步 → 销毁

#### 4.2.1 概念说明

数据准备好后，进入 SiP 特有的部分。SiP 的算子调用不是"一个函数吃进所有参数"这么简单，而是把**一次性准备**和**每次执行**分开：

- **handle**：算子会话对象，持有 plan、workspace、stream 等全部执行资源。
- **Plan（执行计划）**：算子根据自身特性预先算好的执行方案（比如数据怎么切分给 NPU 的各个核）。点积的 plan 不需要额外参数，所以 `asdBlasMakeDotPlan(handle)` 只传句柄。
- **workspace（工作空间）**：算子执行过程中可能需要的 Device 侧临时内存。框架不替你申请，而是遵循"**查询大小 → 你来申请 → 绑定给句柄**"的三步契约——这样你可以用自己管理的内存池。
- **同步**：执行是异步的，读结果前必须 `asdBlasSynchronize`。

这套"句柄式"接口的收益：plan 和 workspace 都只弄一次，之后同一个 handle 可以成千上万次地执行算子，摊薄初始化成本。这正是 u1-l1 提到的「句柄→MakePlan→workspace→绑流→执行→同步→销毁」固定套路的落地。

#### 4.2.2 核心流程

Demo 主流程（[example/example.cpp:L116-L138](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L116-L138)）的固定顺序：

| 步骤 | 接口 | 作用 | 声明位置 |
| --- | --- | --- | --- |
| 1 | `asdBlasCreate(handle)` | 创建句柄（只分配基础结构，不初始化） | [blas_api.h:L34](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L34) |
| 2 | `asdBlasMakeDotPlan(handle)` | 为 dot 算子建立执行计划 | [blas_api.h:L57](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L57) |
| 3 | `asdBlasGetWorkspaceSize(handle, lwork)` | 查询所需 workspace 字节数 | [blas_api.h:L94](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L94) |
| 4 | `aclrtMalloc(&buffer, lwork, ...)` | 用 ACL 申请 workspace 显存 | （ACL 接口） |
| 5 | `asdBlasSetWorkspace(handle, buffer)` | 把 workspace 绑给句柄 | [blas_api.h:L97](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L97) |
| 6 | `asdBlasSetStream(handle, stream)` | 绑定任务流 | [blas_api.h:L37](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L37) |
| 7 | `asdBlasSdot(handle, n, x, incx, y, incy, result)` | 执行点积（异步下发） | [blas_api.h:L147-L148](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L147-L148) |
| 8 | `asdBlasSynchronize(handle)` | 等待计算完成 | [blas_api.h:L103](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L103) |
| 9 | `asdBlasDestroy(handle)` | 销毁句柄、释放其内部资源 | [blas_api.h:L100](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L100) |

其中点积的数学定义（与官方样例文档一致）：

\[ \text{result} = \sum_{i=0}^{n-1} x_i \cdot y_i \]

#### 4.2.3 源码精读

- **创建句柄**（[example/example.cpp:L116-L118](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L116-L118)）：`asdBlasHandle` 就是一个 `void*`，`asdBlasCreate` 只创建"不透明"的骨架。头文件注释（[blas_api.h:L32-L34](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L32-L34)）特别说明：它只分配基础数据结构、不做初始化，初始化由 MakePlan 完成。
- **plan + workspace 契约**（[example/example.cpp:L120-L128](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L120-L128)）：先 MakePlan，再查询 `lwork`；Demo 里有一个细节——只有 `lwork > 0` 才真正 `aclrtMalloc`，否则 `buffer` 保持 `nullptr` 也会照常传给 `asdBlasSetWorkspace`（该算子可能不需要工作空间）。
- **绑流与执行**（[example/example.cpp:L130-L135](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L130-L135)）：`asdBlasSdot` 的参数是经典 BLAS Level 1 风格——`n` 是元素个数，`incx`/`incy` 是步进（允许隔元素取），`result` 是输出张量。注释写明"固定调用逻辑"。
- **销毁与取回**（[example/example.cpp:L137-L148](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L137-L148)）：先销毁句柄，再用 `ACL_MEMCPY_DEVICE_TO_HOST` 把结果从 Device 拷回 `resultData` 并打印。注意所有 asdBlas 接口都返回 `AspbStatus`（u1-l1 介绍过的返回码），Demo 为简洁起见忽略了返回值，工程代码应当检查（u2-l3 专讲）。
- **资源释放**（[example/example.cpp:L150-L164](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L150-L164)）：按"销毁张量描述符 → 释放显存 → 释放 workspace → 销毁流 → 重置设备 → aclFinalize"的逆序收尾，与开头严格对称。

Demo 的默认输入：x = {1,2,3,4,5}（[L75-L77](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L75-L77)），y = {10,11,12,13,14}（[L81-L84](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L81-L84)），因此期望输出为 \(1×10+2×11+3×12+4×13+5×14=190\)。

#### 4.2.4 代码实践

1. **实践目标**：把输入向量改成 8 个元素，用手算结果验证 NPU 计算正确性。
2. **操作步骤**：
   - 修改 [example/example.cpp:L68](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L68) 的 `n = 5` 为 `n = 8`；[L72](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L72) 的 `xSize = 5` 为 `xSize = 8`；[L79](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L79) 的 `ySize = 5` 为 `ySize = 8`。三处必须一起改——注意 [L81](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L81) 处 y 向量 resize 用的是 `xSize`，只改一处会出现"y 长度与填充不一致"的隐患。
   - 手算期望值：x = {1..8}，y = {10..17}，
     \[ \text{result} = \sum_{i=0}^{7} (1+i)(10+i) = 10+22+36+52+70+90+112+136 = 528 \]
   - 重新编译运行（见 4.3.4），观察 `------- result -------` 一行。
3. **需要观察的现象**：程序打印的 result 应为 528。
4. **预期结果**：输出 = 528 即验证通过；若得到 190，说明只改了部分变量。本实践需在配备昇腾 NPU 且已按 u1-l3/u1-l4 装好环境的机器上进行，运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `asdBlasSynchronize(handle)` 这一行删掉，程序会怎样？
**答案**：`asdBlasSdot` 是异步下发，删掉同步后主线程可能在实际计算完成前就把 result 拷回 Host，得到未写完的随机值；也可能碰巧算完而"看起来正常"，属于典型的隐式竞态，调试期极难定位。

**练习 2**：为什么 workspace 由用户申请（`aclrtMalloc`）而不是库内部自己申请？
**答案**：三步契约（查询大小→用户申请→绑定）把内存管理权交给调用方：批量场景下可以复用一块大缓冲区服务多个算子，也可以接入自己的内存池，避免库内部反复 malloc/free 带来的碎片与性能抖动。

**练习 3**：同一个 handle 能否连续执行两次 `asdBlasSdot`（换不同的输入张量）？
**答案**：可以。plan/workspace/stream 都挂在 handle 上，执行接口只接收张量与规模参数，因此"一次准备、多次执行"正是这套设计的意图；只需在最后统一 `asdBlasSynchronize` 再读结果。

### 4.3 编译运行：build.sh 与 A2 样例地图

#### 4.3.1 概念说明

跑通 Demo 需要两套环境同时就位：CANN 环境（提供 ACL 头文件与 `libascendcl` 等库）和 SiP 环境（`ASDSIP_HOME_PATH` 指向的安装目录，提供 `asdsip.h` 与四个 so）。`example/build.sh` 把这两套环境的检查和 g++ 命令打包成一键脚本。

同时，`example/A2/` 下藏着一份宝藏：按 **BASE / BLAS / Domain / FFT / Filter / Interpolation** 六个模块组织的全部算子专属样例，每个算子目录内含 README（功能与公式）、build.sh 和若干 `example_*.cpp`（一个具体接口一个文件）。学会查这份地图，你就能自己跑通仓库里几乎所有算子。

#### 4.3.2 核心流程

```text
前置（一次性）：
  source ${CANN路径}/set_env.sh      # 得到 ASCEND_HOME_PATH
  cd ${SiP根目录} && bash build.sh    # 编译 SiP（u1-l3）
  source output/set_env.sh            # 得到 ASDSIP_HOME_PATH（u1-l4）

每次运行 Demo：
  cd example && bash build.sh
    ├─ 检查 ASDSIP_HOME_PATH 已设置且目录存在（兼容 latest 软链写法）
    ├─ 把 $ASDSIP_HOME_PATH/lib 追加进 LD_LIBRARY_PATH
    ├─ g++ 编译 example.cpp（CANN 与 SiP 的 -I/-L 各就各位）
    └─ ./example 运行后删除临时可执行文件
```

#### 4.3.3 源码精读

- **环境检查**（[example/build.sh:L12-L16](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh#L12-L16)）：`ASDSIP_HOME_PATH` 未设置直接报错退出——这就是 u1-l4 强调"必须先 source set_env.sh"的原因。
- **软链兼容**（[example/build.sh:L18-L28](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh#L18-L28)）：若变量指向的目录不存在，会尝试剥掉结尾的 `latest` 再检查，并把 `lib` 子目录追加进 `LD_LIBRARY_PATH`，运行期靠它找到动态库。
- **编译命令**（[example/build.sh:L30-L39](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh#L30-L39)）：g++ 一行式，两套环境各占一半——CANN 侧 `-I${ASCEND_HOME_PATH}/include`（及 `include/aclnn`）+ `-lascendcl -lopapi -lnnopbase`；SiP 侧 `-I${ASDSIP_HOME_PATH}/include` + `-lmki -lasdsip -lasdsip_core -lasdsip_host`。对照 u1-l4 的结论：理论上只链 `libasdsip.so` 即可（其余由它依赖），Demo 显式链接全部四个是更稳妥的写法。
- **官方三步说明**（[example/README.md:L9-L43](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/README.md#L9-L43)）：环境配置 → SiP 编译 → 运行 demo，与本讲流程一致；README 还提醒 zip 包下载的源码不支持该编译方式、编译需联网拉取 ascend-boost-comm 依赖。
- **A2 样例地图**：`example/A2/` 下六个模块子目录与 include 下的六个头文件一一对应。以 dot 为例（[example/A2/BLAS/dot/](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/A2/BLAS/dot/README.md)）：README 的功能说明与公式在 [L9-L15](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/A2/BLAS/dot/README.md#L9-L15)（Sdot 公式即本讲 4.2.2 的求和式）；目录内 `example_sdot.cpp`、`example_cdotu.cpp`、`example_cdotc.cpp` 分别对应 `asdBlasSdot/Cdotu/Cdotc` 三个接口。其 build.sh 与主样例几乎逐字相同，唯一差别是把编译对象从 `example.cpp` 换成 `example_sdot.cpp`。
- **CMake 备选**：`example/CMakeLists.txt` 提供了另一条构建路径，当前生效的是第 [61](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/CMakeLists.txt#L61) 行的 `file(GLOB_RECURSE SOURCE_FILES example.cpp)`、第 [L63-L65](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/CMakeLists.txt#L63-L65) 行的可执行目标，以及第 [79-L85](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/CMakeLists.txt#L79-L85) 行链接 CANN 三个 so 并安装的动作；文件前半部分大量被注释的 GLOB 行，恰好是 A2 各样例文件名的一览表。

#### 4.3.4 代码实践

1. **实践目标**：独立跑通主样例，并学会在 A2 地图里找到并运行其他算子的样例。
2. **操作步骤**：
   - 前置：完成 u1-l3 编译、u1-l4 环境设置后，依次执行 `source /usr/local/Ascend/ascend-toolkit/set_env.sh`（按实际 CANN 路径）与 `source ${SiP根目录}/output/set_env.sh`。
   - 主样例：`cd example && bash build.sh`，确认输出 190。
   - 专属样例：`cd example/A2/BLAS/dot && bash build.sh`，运行同一算子的独立样例 `example_sdot.cpp`。
3. **需要观察的现象**：两个脚本都会先输出编译动作，再打印输入向量与 `------- result -------`；dot 样例的提示语是"performing vector dot product operations"。
4. **预期结果**：两处结果一致（190）。若无 NPU 环境，本步骤**待本地验证**；但下面这条可以离线完成——对比 [example/A2/BLAS/dot/build.sh](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/A2/BLAS/dot/build.sh) 与 [example/build.sh](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh)，确认仅编译源文件与提示语两处不同，从而得出结论：**把任何一个 `example_*.cpp` 换进这条 g++ 命令就能编译运行**。

#### 4.3.5 小练习与答案

**练习 1**：想运行 `asdBlasSasum`（绝对值求和）的样例，应该去哪里找、怎么跑？
**答案**：BLAS 模块 → `example/A2/BLAS/asum/` 目录，内含 `example_sasum.cpp` 与 `example_scasum.cpp`；进入该目录执行 `bash build.sh` 即可（其脚本同样只把编译对象换成 `example_sasum.cpp`）。

**练习 2**：执行 `bash build.sh` 报错 "the env params ASDSIP_HOME_PATH is not set."，最可能漏了什么？
**答案**：漏了 source SiP 的环境脚本。应先 `source ${SiP安装目录或output目录}/set_env.sh`（u1-l4 讲过它会导出 `ASDSIP_HOME_PATH` 并追加 `LD_LIBRARY_PATH`），必要时还需先 source CANN 的 set_env.sh 以获得 `ASCEND_HOME_PATH`。

**练习 3**：`example/build.sh` 最后一行为什么要 `rm example`？
**答案**：脚本是"编译即运行"的一次性流程，运行完删除临时可执行文件保持目录干净；这也提示我们：想保留可执行文件做反复调试时，应把 g++ 命令单独拿出来执行，或注释掉该行。

## 5. 综合实践

**任务：给 asdBlasSdot 写一份「改动-预测-验证」实验报告。**

1. 在纸上写出 Demo 的 10 步调用序列（ACL 初始化 3 步 + SiP 7 步），并标注每一步对应 [include/blas_api.h](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h) 中的声明行号。
2. 完成本讲 4.2.4 的改动（n/xSize/ySize 同步改为 8），先手算期望值 528 并写下推导过程。
3. 在有 NPU 的机器上编译运行，把实际输出与预测对照；再故意只改 `xSize` 不改 `n`，运行并解释观察到的结果（提示：`n=5` 时只有前 5 个元素参与点积，期望值回到 190 量级，同时 y 的 resize 用了 `xSize` 会出现 3 个未填充元素——这正是"参数必须成组修改"的教训）。
4. 把实验中每一步的返回码打印出来（`std::cout << asdBlasCreate(handle) << std::endl;`，示例代码），为 u2-l3 学习 AspbStatus 错误处理积累感性认识。

本综合实践把本讲三个模块（ACL 初始化、Demo 主流程、编译运行）串成一条完整链路；若暂无 NPU 环境，第 1、2 步与第 4 步的代码修改仍可完成，运行结果标注**待本地验证**即可。

## 6. 本讲小结

- ACL 三连 `aclInit → aclrtSetDevice → aclrtCreateStream` 是所有昇腾程序的固定开场；`CreateAclTensor` 封装了"malloc → H2D 拷贝 → 算 strides → aclCreateTensor"四步。
- SiP 算子调用固定套路：**Create 句柄 → MakePlan → GetWorkspaceSize/申请/SetWorkspace → SetStream → Exec → Synchronize → Destroy**，plan 与 workspace 一次准备、多次执行。
- 执行是异步的：读结果前必须 `asdBlasSynchronize`，再以 `ACL_MEMCPY_DEVICE_TO_HOST` 拷回。
- workspace 遵循"查询大小 → 用户申请 → 绑定句柄"三步契约，`lwork` 为 0 时可不申请。
- `example/build.sh` 一键完成环境检查 + g++ 编译 + 运行；CANN 与 SiP 两套 `-I/-L` 缺一不可。
- `example/A2/` 按六模块组织全部算子专属样例，任一 `example_*.cpp` 换入同款 g++ 命令即可运行。

## 7. 下一步学习建议

下一讲进入第二单元「公共接口与基础机制」：建议先读 [include/asdsip.h](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/asdsip.h)（u2-l1 将系统梳理六大模块头文件与接口前缀），再带着本讲的两个疑问往下走：① `AspbStatus` 返回码怎么用（u2-l3）；② Demo 里那些被忽略的返回值在工程代码里如何检查。同时可以在 `example/A2/BLAS/dot/` 里对比 `example_cdotu.cpp`，提前感受复数张量（ACL_COMPLEX64）与 float 张量在创建时的差异——这会为 u2-l4 的 Tensor 基础设施做铺垫。
