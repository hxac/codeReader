# 公开 API 全景：asdsip.h 与六大模块头文件

## 1. 本讲目标

学完本讲，你应该能够：

1. 脱口说出 SiP 的六大公开 API 模块（Base / BLAS / FFT / Filter / Interpolation / Domain）与前缀的对应关系。
2. 看到一个接口名（如 `asdBlasHCgemmBatched`、`asdFftExecC2R`），能立刻推断出：它属于哪个模块、在哪个头文件里声明、需要链接哪个库。
3. 按需 include 正确的单模块头文件，而不是无脑 `#include "asdsip.h"`，并理解两种 API 风格（有柄的 Handle/Plan 风格与无柄的直调风格）。
4. 说清「一个总入口 + 六个模块头文件 + 一个主库 libasdsip.so」这层对应关系，以及 `include/` 目录下哪些文件其实不是公开 API。

本讲不深入任何算子的实现，只建立**接口地图**——它是后续所有模块讲义（u7 BLAS、u8 FFT、u9 融合算子）的公共索引。

## 2. 前置知识

本讲要用到几个 C++ 工程层面的基础概念，先用两三句话解释清楚：

- **伞形头文件（umbrella header）**：一个自身几乎没有内容、只做 `#include` 转发的头文件。用户 include 它一个，就等于把它聚合的所有头文件都 include 了一遍。SiP 的 `asdsip.h` 就是这种设计。
- **前向声明（forward declaration）**：`typedef struct aclTensor aclTensor;` 只告诉编译器「存在一个叫 aclTensor 的结构体」，不给出它的成员。因为头文件里只用 `aclTensor *` 指针，指针不需要完整类型定义，这样就能避免引入沉重的依赖头文件。
- **`extern "C"` 与名字改编（name mangling）**：C++ 编译器默认会把函数名加上命名空间、参数类型等信息改编成奇怪的长符号。`extern "C"` 告诉编译器「这段函数用 C 链接」，符号名保持原样。我们后面会用 `nm` 工具实际观察这个差异。
- **动态库与 `-l` 链接**：Linux 下 `.so` 是动态库。`-lasdsip` 让链接器去找 `libasdsip.so`（把 `-l` 后的名字加上 `lib` 前缀和 `.so` 后缀），并用 `-L` 指定搜索目录。

同时承接前几讲的术语：所有 SiP 接口都在 `AsdSip` 命名空间下（u1-l1）；返回值统一是 `AspbStatus`（本质是 `int32_t`，详见 u2-l3）；张量统一用 CANN 的 `aclTensor *` 描述（u1-l5）；BLAS/FFT 算子遵循「句柄→MakePlan→workspace→绑流→执行→同步→销毁」套路（u1-l5）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `include/asdsip.h` | 总入口伞形头文件，聚合六个模块头文件 | 4.1 主角 |
| `include/base_api.h` | Base 模块：轴交换、逐元素乘 | 4.2 直调风格样本 |
| `include/blas_api.h` | BLAS 模块：线性代数全家桶，接口最多 | 4.2 有柄风格样本一 |
| `include/fft_api.h` | FFT 模块：1D/2D/3D、C2C/C2R/R2C、STFT/ISTFT | 4.2 有柄风格样本二 |
| `include/filter_api.h` | Filter 模块：一维卷积 | 4.2 直调风格样本 |
| `include/interp_api.h` | Interpolation 模块：基于系数的插值 | 4.2 直调风格样本 |
| `include/domain/rs_api.h` | Domain 模块：雷达场景 Sinc 插值 | 4.2 直调风格样本 |
| `include/blas_common.h` | BLAS 模块**内部**聚合头（不在公开 API 之列） | 4.1 反面教材 |
| `core/utils/include/utils/aspb_status.h` | 定义 `AspbStatus`，安装后位于 `include/utils/` | 4.2 公共依赖 |
| `core/utils/include/utils/mem_base.h` | `aclTensor` 前向声明，安装后位于 `include/utils/` | 4.2 公共依赖 |
| `docs/header_files_library_files.md` | 官方「头文件-库文件」对照文档 | 4.3 权威依据 |
| `example/build.sh` | 官方示例的编译链接脚本 | 4.3 链接实战参照 |

> 注意：`aspb_status.h` 和 `mem_base.h` 在仓库里位于 `core/utils/include/utils/`，安装时会被复制到安装目录的 `include/utils/` 下，所以源代码里写 `#include "utils/aspb_status.h"` 在用户侧也能编译通过。

## 4. 核心概念与源码讲解

### 4.1 总入口头文件

#### 4.1.1 概念说明

SiP 把全部公开接口分散在六个模块头文件里，又提供一个**总入口** `asdsip.h` 把它们聚合起来。这带来两种使用姿势：

- **省心姿势**：`#include "asdsip.h"`，一行拿到全部接口。代价是把六大模块连同它们依赖的 CANN 头文件（`acl/acl.h`、`<complex>`、`aclnn/opdev/fp16_t.h` 等）全部拉进你的编译单元。
- **精准姿势**：只用哪个模块就 include 哪个头文件，比如只做卷积就 `#include "filter_api.h"`。编译更快、依赖更少，也强迫你清楚自己用了什么。

判断「哪些头文件属于公开 API」的唯一权威标准，就是看 `asdsip.h` 聚合了谁——这是本讲要建立的核心直觉。

#### 4.1.2 核心流程

`asdsip.h` 的预处理展开过程可以用伪代码描述：

```text
#include "asdsip.h"
        │
        ├── #include "base_api.h"     ──→ utils/aspb_status.h + acl/acl.h + utils/mem_base.h
        ├── #include "blas_api.h"     ──→ <complex> + aclnn/opdev/fp16_t.h + acl/acl.h + 两个 utils 头
        ├── #include "fft_api.h"      ──→ utils/aspb_status.h（自前向声明 aclTensor，不需要 acl/acl.h）
        ├── #include "filter_api.h"   ──→ utils/aspb_status.h + acl/acl.h + utils/mem_base.h
        ├── #include "interp_api.h"   ──→ acl/acl.h + utils/aspb_status.h + utils/mem_base.h
        └── #include "domain/rs_api.h"──→ utils/aspb_status.h + acl/acl.h + utils/mem_base.h
```

可见：六个头文件都依赖 `utils/aspb_status.h`（返回值类型），五个依赖 `acl/acl.h`（完整张量类型），只有 `fft_api.h` 例外——它自己做了前向声明。只有 `blas_api.h` 额外依赖 CANN 的 aclnn 头（为了 `fp16_t` 半精度类型）。这些差异直接决定了 4.2.4 实践中每个头文件需要的 `-I` 路径不同。

#### 4.1.3 源码精读

总入口本身就是全仓库最短的公开头文件，去掉版权注释后只有不到 10 行：

[include/asdsip.h:L10-L21](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/asdsip.h#L10-L21) —— 头文件保护宏 `ASDSIP_API_H` 之后，就是六行 `#include`，依次聚合 `base_api.h`、`blas_api.h`、`fft_api.h`、`filter_api.h`、`interp_api.h`、`domain/rs_api.h`，此外一行多余的内容都没有。这六行就是 SiP 公开 API 的完整清单。

再看官方示例是怎么用它的：

[example/example.cpp:L12-L17](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/example.cpp#L12-L17) —— 示例程序同时 `#include "asdsip.h"`、`"acl/acl.h"`、`"acl_meta.h"`：SiP 的伞形头给全了 SiP 接口，但 ACL 初始化（`aclInit`、`aclrtSetDevice` 等）仍需用户自己 include CANN 头文件——SiP 头文件只管声明算子，不管运行时初始化。

`include/` 目录下还有第 8 个头文件 `blas_common.h`，它**不在**聚合清单里：

[include/blas_common.h:L14-L24](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_common.h#L14-L24) —— 它除了 include 公开的 `blas_api.h`，还引入 `utils/ops_base.h`、`log/log.h` 等内部头，甚至用 `#include "../blas/blasplan/include/blasplan/BlasPlan.h"` 这种**跳出 include 目录的相对路径**引用 core 内部实现。它是 BLAS 模块实现代码用的内部聚合头，普通用户不应也无法单独依赖它。结论：**文件放在 `include/` 下不等于就是公开 API，以 `asdsip.h` 的聚合清单为准**。

#### 4.1.4 代码实践

**实践目标**：验证 `asdsip.h` 聚合的完整性——只 include 它，就能同时使用来自不同模块的接口（FFT 的枚举、Filter 的枚举、BLAS 的句柄类型）。

**操作步骤**（示例代码）：

```cpp
// tu_umbrella.cpp —— 验证总入口聚合完整性（示例代码）
#include "asdsip.h"

int main()
{
    // 三个成员分别来自 fft_api.h、filter_api.h、blas_api.h
    AsdSip::asdFftType t = AsdSip::ASCEND_FFT_C2C;
    AsdSip::asdConvolveMode_t m = AsdSip::asdConvolveMode_t::ASD_CONVOLVE_FULL;
    AsdSip::asdBlasHandle h = nullptr;
    return static_cast<int>(t) + static_cast<int>(m) + (h != nullptr);
}
```

前提：已按 u1-l4 安装 SiP 并 `source set_env.sh`，同时 `source` 了 CANN 环境使得 `$ASDSIP_HOME_PATH`、`$ASCEND_HOME_PATH` 生效。编译命令参照官方示例 [example/build.sh:L30-L33](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh#L30-L33) 的写法：

```bash
g++ -c tu_umbrella.cpp \
    -I${ASDSIP_HOME_PATH}/include \
    -I${ASCEND_HOME_PATH}/include \
    -I${ASCEND_HOME_PATH}/include/aclnn \
    -o tu_umbrella.o
```

**需要观察的现象**：编译单元一次通过，不需要再 include 任何 SiP 模块头文件；如果故意把 `#include "asdsip.h"` 改成 `#include "fft_api.h"`，`asdConvolveMode_t` 一行应立刻报「未声明」错误。

**预期结果**：聚合头生效，三种符号全部可见。此实验只需要编译（`-c`），不需要 NPU，任何装好 CANN 头文件与 SiP 安装包的机器都能做。编译通过与否的最终结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：只想调用 `asdConvolve`，用 `asdsip.h` 和 `filter_api.h` 都能编译通过，两者在预处理层面差在哪？

**答案**：`asdsip.h` 会把六个模块头全部展开，连带 `<complex>`、`aclnn/opdev/fp16_t.h`、`acl/acl.h` 等全部依赖；`filter_api.h` 只展开它自己和三个依赖（`utils/aspb_status.h`、`acl/acl.h`、`utils/mem_base.h`），编译单元更小，且不需要提供 aclnn 的 include 路径。

**练习 2**：`include/` 目录下的 `blas_common.h` 为什么不算公开 API？

**答案**：两个判据——(1) 它不在 `asdsip.h` 的聚合清单里；(2) 它的内容依赖内部实现头（`utils/ops_base.h`、`log/log.h`），并用相对路径 `../blas/blasplan/...` 引用了 include 目录之外的 core 内部文件，普通用户的安装目录里根本不存在这些路径。

### 4.2 六大模块头文件

#### 4.2.1 概念说明

六个头文件背后是两类截然不同的 API 设计风格，先建立这个分类，后面读任何接口都不会迷路：

| 风格 | 特征 | 成员模块 | 典型调用形态 |
| --- | --- | --- | --- |
| **有柄风格**（Handle/Plan） | 有句柄类型、有 MakePlan 系列、执行前要准备 workspace | BLAS、FFT | Create → SetStream → MakeXxxPlan → GetWorkspaceSize → SetWorkspace → Exec → Synchronize → Destroy |
| **直调风格**（无柄直调） | 没有句柄、不建 plan，stream 直接作参数传入，workspace 可为空指针 | Base、Filter、Interpolation、Domain | （可选）GetWorkspaceSize → 一次函数调用收工 |

为什么会有两种风格？有柄风格面向**形状/参数固定、反复执行**的场景（例如雷达处理流水线里同样尺寸的 FFT 要做百万次），plan 与 workspace 一次准备、多次复用；直调风格面向**一次性、轻量**的操作（如一次卷积、一次轴交换），为省去状态管理把接口做成「函数即服务」。这个设计动机在 u2-l2 会展开成完整编程模型。

六大模块与前缀的对应关系（与官方文档 `docs/header_files_library_files.md` 的接口分类表一致）：

| 前缀 | 模块 | 头文件 | 风格 | 代表接口 |
| --- | --- | --- | --- | --- |
| `asdFft*` | FFT | `fft_api.h` | 有柄 | `asdFftMakePlan1D`、`asdFftExecC2C` |
| `asdBlas*` | BLAS | `blas_api.h` | 有柄 | `asdBlasCgemm`、`asdBlasSdot` |
| `asdConvolve*` | Filter | `filter_api.h` | 直调 | `asdConvolve` |
| `asdInterp*` | Interpolation | `interp_api.h` | 直调 | `asdInterpWithCoeff` |
| `swapLast2Axes` / `asdMul` | Base | `base_api.h` | 直调 | `swapLast2Axes`、`asdMul` |
| `rs*` | Domain | `domain/rs_api.h` | 直调 | `rsInterpolationBySinc` |

注意 Base 是个命名上的「例外」：它的接口没有统一的 `asd` 前缀（`swapLast2Axes` 裸奔，`asdMul` 有前缀），官方文档的接口分类表也因此把 Base 模块的前缀写作「swapLast2Axes / asdMul」。记忆时以「模块 → 头文件」的映射为主，前缀为辅。

#### 4.2.2 核心流程

**（1）BLAS 接口名的解码公式**。BLAS 命名沿用业界 BLAS 库的传统，首位字母编码数据类型：

\[ \underbrace{\text{asdBlas}}_{\text{库前缀}} + \underbrace{S/C/H}_{\text{数据类型}} + \underbrace{\text{操作名}}_{\text{如 gemm、dot、asum}} + \underbrace{\text{Batched}}_{\text{可选：批量}} \]

| 首位字母 | 类型 | C++ 表达 | 例 |
| --- | --- | --- | --- |
| `S` | 单精度浮点 | `float` | `asdBlasSdot`、`asdBlasSasum` |
| `C` | 单精度复数 | `std::complex<float>` | `asdBlasCgemm`、`asdBlasCdotc` |
| `H` | 半精度（fp16） | `std::complex<op::fp16_t>` | `asdBlasHCgemmBatched` |
| `I`（Isamax/Icamax） | 返回下标 | 结果为 int | `asdBlasIsamax`、`asdBlasIcamax` |

于是 `asdBlasHCgemmBatched` 一眼可读：H（半精度）+ Cgemm（复数矩阵乘）+ Batched（批量）。`Scasum` 这种复合名按 BLAS 惯例拆成 S + casum（复数向量的绝对值之和，返回 float）。

**（2）有柄风格的接口生命周期**。以 BLAS 为例，头文件中的声明顺序就是生命周期顺序：

```text
asdBlasCreate ─→ asdBlasSetStream ─→ asdBlasMake<Op>Plan(静态参数)
                                    ─→ asdBlasGetWorkspaceSize ─→ 用户申请 ─→ asdBlasSetWorkspace
                                    ─→ asdBlas<Op>(张量参数，可反复执行)
                                    ─→ asdBlasSynchronize ─→ asdBlasDestroy
```

关键分工：**MakePlan 固化「不会变的静态参数」**（矩阵尺寸 m/n/k、转置模式、leading dimension），**Exec 只传「每次执行变化的数据张量」**（A、B、C 指针）。这就是 plan 能被反复复用的原因。

**（3）FFT 枚举取值的分组**。`asdFftType` 的取值按高 4 位分组：`0x1x` 是普通 FFT 族（C2C/C2R/R2C），`0x2x` 是 STFT 族，`0x30` 是分离存储（real/imag 分开）的 C2C。从取值分布能看出类型维度的正交设计：变换家族 × 数据方向 × 存储方式。

#### 4.2.3 源码精读

**① `fft_api.h`——最「自轻」的头文件**。

[include/fft_api.h:L15-L19](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/fft_api.h#L15-L19) —— 这是六个公开头中唯一用 `extern "C"` 包裹的（C 链接，符号不做 C++ 名字改编），并且在 L19 用 `typedef struct aclTensor aclTensor;` 前向声明张量类型，**因此它不需要 include `acl/acl.h`**——六个头文件里依赖最少的一个。

[include/fft_api.h:L22-L44](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/fft_api.h#L22-L44) —— 定义句柄 `typedef void *asdFftHandle;`（不透明指针，内部实现用户不可见）和三组**非强类型枚举**（`enum asdFftType`、`asdFftDirection`、`asdFft1dDimType`，注意没有 `class`，与 BLAS 的 `enum class` 形成对比——非强类型枚举对 C 链接更友好）。

[include/fft_api.h:L46-L62](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/fft_api.h#L46-L62) —— 生命周期入口逐条带注释声明：`asdFftCreate` 只分配基础结构不初始化；`asdFftMakePlan1D` 携带 fftSize/类型/方向/batchSize/dimType 五个静态参数初始化句柄（注释明确「一个句柄只能初始化一次」）；`asdFftIstftMakePlan` 则直接接收 `aclTensor *input`——用输入张量自身描述 ISTFT 参数，是有柄家族里比较特殊的 plan。

[include/fft_api.h:L65-L94](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/fft_api.h#L65-L94) —— Exec 家族按「输出类型」分派：`asdFftExecC2C`（复数→复数）、`asdFftExecC2CSeparated`（实部虚部分开存储，接收四个张量）、`asdFftExecC2R`/`asdFftExecR2C`（复↔实转换）、`asdFftExecIstft`；随后是 `asdFftMakePlan2D/3D`、`asdFftGetWorkspaceSize/SetWorkspace/Synchronize/GetType`，把 1.2 节的生命周期表完整落地。

**② `blas_api.h`——接口最密集的头文件（约 190 行声明）**。

[include/blas_api.h:L20-L30](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L20-L30) —— 句柄定义 `using asdBlasHandle = void *;`，以及五个**强类型枚举**（`enum class`）：`asdBlasStatus`、`asdBlasSideMode_t`（左乘/右乘）、`asdBlasOperation_t`（N 不转置/T 转置/C 共轭转置）、`asdBlasFillMode_t`（下/上三角/全矩阵）、`asdBlasDiagType_t`（单位对角与否）。这组枚举对应经典 BLAS 的 UPLO/TRANSA/DIAG 概念。

[include/blas_api.h:L34-L91](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L34-L91) —— 生命周期前半段：`asdBlasCreate`、`asdBlasSetStream` 之后是一长串 `asdBlasMake<Op>Plan`。注意哪些 plan 带参数、哪些不带：`asdBlasMakeCgemmPlan` 携带 transa/transb/m/n/k/lda/ldb/ldc 八个静态参数（L45-L46），而 `asdBlasMakeDotPlan`、`asdBlasMakeAsumPlan` 等向量级 plan 无参数（向量长度 n 留到 Exec 传）。**plan 参数表本身就告诉你该算子的「静态形状」是什么**。

[include/blas_api.h:L112-L114](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L112-L114) —— 执行接口 `asdBlasCgemm` 的签名：alpha/beta 是 `std::complex<float>` 标量，A/B/C 是 `aclTensor *`，lda/ldb/ldc 是 leading dimension。与 L45 的 MakeCgemmPlan 对照可见 Exec 不再传 m/n/k——它们已被 plan 固化。

[include/blas_api.h:L198-L207](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L198-L207) —— 批量家族收尾：`asdBlasCgemmBatched` 多出 `batchCount` 参数；`asdBlasCmatinvBatched`（批量复数矩阵求逆）接收 A/Ainv/info 三个张量与 batchSize——矩阵求逆这类 Solver 级能力也是以 BLAS 接口形态暴露的（u7-l5 详讲）。

**③ `base_api.h`——直调风格最小样本**。

[include/base_api.h:L18-L24](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/base_api.h#L18-L24) —— 整个头文件的公开接口只有三个：`swapLast2AxesGetWorkspaceSize`、`swapLast2Axes`、`asdMul`。注意它们的签名模式：`void *stream` 直接作为参数，`void *workspace = nullptr` 给了默认值——不建 plan、不需要句柄，一个函数调用即完成。`GetWorkspaceSize` 与执行函数成对出现，是直调家族的标准搭配。

**④ `filter_api.h`——一枚枚举 + 一对函数**。

[include/filter_api.h:L20-L30](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/filter_api.h#L20-L30) —— `asdConvolveMode_t` 定义 full/same/valid 三种卷积模式（与 numpy/MATLAB 的同名模式语义一致，输出长度规则在 u9-l1 详解）；`asdConvolveGetWorkspaceSize` 的参数是两个整数长度（signalLen/kernelLen）而非张量——workspace 大小只取决于数据长度，这也是直调家族的典型特征。

**⑤ `interp_api.h` 与 ⑥ `domain/rs_api.h`——同构的直调双子星**。

[include/interp_api.h:L19-L21](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/interp_api.h#L19-L21) —— `asdInterpWithCoeff(x, coefficient, output, stream, workspace)` + 对应的 `GetWorkspaceSize`，两行就是全部。

[include/domain/rs_api.h:L19-L24](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/domain/rs_api.h#L19-L24) —— 雷达领域的 `rsInterpolationBySinc` 接收 sincTab/posFloor/posToTabIndex 三个预计算张量加 interpNum/quantNum/interpLength 三个整数参数，同样是「执行函数 + GetWorkspaceSize」成对出现。一个有意思的细节：它的头文件保护宏叫 `ASDSIP_SIGNAL_API_H`（rs_api.h:L10），透露了 Domain 模块的前身命名是「signal」。

**⑦ 两个公共依赖**。

[core/utils/include/utils/aspb_status.h:L16](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/utils/include/utils/aspb_status.h#L16) —— `using AspbStatus = int32_t;`，六个头文件里所有接口的返回值都是它。

[core/utils/include/utils/mem_base.h:L13](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/core/utils/include/utils/mem_base.h#L13) —— 整个文件只有一行 `typedef struct aclTensor aclTensor;` 前向声明，是「最小依赖」技巧的极致体现。

#### 4.2.4 代码实践

**实践目标**：验证六个模块头文件**各自都能独立编译**，并亲手发现它们的依赖差异（这正是本讲的实践任务核心）。

**操作步骤**（以下均为示例代码，文件名自取）：

第一步，写三个最小翻译单元（另外三个见综合实践）：

```cpp
// tu_fft.cpp —— 只 include fft_api.h（示例代码）
#include "fft_api.h"

int main()
{
    AsdSip::asdFftHandle handle = nullptr;
    AsdSip::AspbStatus ret = AsdSip::asdFftCreate(handle);  // 只验证可编译、可链接
    if (ret == 0) {
        AsdSip::asdFftDestroy(handle);
    }
    AsdSip::asdFftType t = AsdSip::ASCEND_FFT_C2C;          // 用到枚举
    return static_cast<int>(t);
}
```

```cpp
// tu_filter.cpp —— 只 include filter_api.h（示例代码）
#include "filter_api.h"

int main()
{
    AsdSip::asdConvolveMode_t mode = AsdSip::asdConvolveMode_t::ASD_CONVOLVE_VALID;
    size_t ws = 0;
    // 只取 workspace 大小查询的声明做编译验证
    AsdSip::asdConvolveGetWorkspaceSize(8, 3, ws);
    return static_cast<int>(mode);
}
```

```cpp
// tu_blas.cpp —— 只 include blas_api.h（示例代码）
#include "blas_api.h"

int main()
{
    AsdSip::asdBlasHandle h = nullptr;
    AsdSip::asdBlasOperation_t op = AsdSip::asdBlasOperation_t::ASDBLAS_OP_N;
    AsdSip::asdBlasFillMode_t uplo = AsdSip::asdBlasFillMode_t::ASDBLAS_FILL_MODE_LOWER;
    AsdSip::AspbStatus ret = AsdSip::asdBlasCreate(h);
    if (ret == 0) {
        ret = AsdSip::asdBlasMakeStrmvPlan(h, uplo, op, 8);  // n=8 的三角乘向量 plan
        AsdSip::asdBlasDestroy(h);
    }
    return static_cast<int>(ret);
}
```

第二步，按 4.1.2 分析的依赖差异分别编译（`-c` 只编译不链接，不需要 NPU）：

```bash
# fft_api.h 不依赖 acl/acl.h，理论上只需 SiP 的 include 路径
g++ -c tu_fft.cpp -I${ASDSIP_HOME_PATH}/include -o tu_fft.o

# filter_api.h 依赖 acl/acl.h，需追加 CANN include
g++ -c tu_filter.cpp -I${ASDSIP_HOME_PATH}/include -I${ASCEND_HOME_PATH}/include -o tu_filter.o

# blas_api.h 还依赖 aclnn/opdev/fp16_t.h，需再追加 aclnn 路径（与 example/build.sh 一致）
g++ -c tu_blas.cpp -I${ASDSIP_HOME_PATH}/include -I${ASCEND_HOME_PATH}/include \
    -I${ASCEND_HOME_PATH}/include/aclnn -o tu_blas.o
```

第三步，验证符号链接（以 fft 为例，链接 libasdsip）：

```bash
g++ tu_fft.cpp -I${ASDSIP_HOME_PATH}/include \
    -L${ASDSIP_HOME_PATH}/lib -lasdsip -o tu_fft
```

**需要观察的现象**：

1. 三个 `.o` 都应生成；若给 `tu_fft.cpp` 的命令错误地加上 `-I${ASCEND_HOME_PATH}/include` 也能过——差异体现在**去掉**某个 `-I` 后谁还能活：`tu_fft.o` 在只有 SiS include 路径时仍应编译成功，`tu_blas.o` 缺少 aclnn 路径时应报 `aclnn/opdev/fp16_t.h: No such file`。
2. 链接生成 `tu_fft` 可执行文件本身不需要 NPU（真正的 `asdFftCreate` 执行才需要）。
3. 可选：`nm -D ${ASDSIP_HOME_PATH}/lib/libasdsip.so | grep asdFftCreate`，预期看到未改编的 C 链接符号名（与 `extern "C"` 声明呼应）；再 grep 一个 C++ 链接的 BLAS 符号对比改编差异。

**预期结果**：六个头文件全部可独立编译；依赖路径需求为 `fft`（1 个）< `filter/base/interp/domain`（2 个）< `blas`（3 个）。本实践的全部具体输出待本地验证——尤其在没有 NPU 的机器上请只做 `-c` 编译验证。

#### 4.2.5 小练习与答案

**练习 1**：`asdBlasCdotu` 与 `asdBlasCdotc` 名字里的 C、u、c 各代表什么？

**答案**：首位 `C` = 复数（`std::complex<float>`）；按 BLAS 传统命名，`dotu` 是无共轭点积（dot unconjugated），`dotc` 是共轭点积（dot conjugated，即先对 x 取共轭再相乘累加）。两者语义的精确定义可查 `docs/zh/API_Reference/BLAS/Dot.md`（u7-l3 详解）。

**练习 2**：`fft_api.h` 为什么可以不 include `acl/acl.h`？这样做的代价是什么？

**答案**：因为 [include/fft_api.h:L19](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/fft_api.h#L19) 自己写了 `typedef struct aclTensor aclTensor;` 前向声明，而头文件里只出现 `aclTensor *` 指针，指针类型不需要完整定义。代价是：用户真正要构造 aclTensor 调用 `asdFftExecC2C` 时，仍需自行 include `acl/acl.h` 获得完整类型与操作函数。

**练习 3**：想批量做 32 个 16×16 复数矩阵乘，应选 `asdBlasCgemm` 还是 `asdBlasCgemmBatched`？分别要 include 与链接什么？

**答案**：批量场景选 `asdBlasCgemmBatched`（带 `batchCount` 参数，一次调用下发全部矩阵，u7-l5 详析收益）。无论选哪个，include `blas_api.h`（或总入口 `asdsip.h`），链接 `libasdsip.so`（`-lasdsip`）；若数据是半精度复数则选 `asdBlasHCgemmBatched`。

### 4.3 库文件对应关系

#### 4.3.1 概念说明

一个反直觉的事实：**六个头文件、上百个接口，最终都对应同一个用户库 `libasdsip.so`**。不存在「libasdfft.so」「libasdblas.so」这样的按模块拆库。

为什么不拆？因为 SiP 在**源码层**按模块分层（core/blas、core/fft……），在**交付层**却收敛为一个门面库：用户只需要 `-lasdsip` 一个链接项，内部的分层（`libasdsip_core.so` 算子运行时、`libasdsip_host.so` 主机工具、MKI 框架）由主库自动依赖、对用户透明。这是「宽接口、窄依赖」的典型交付设计——记住一个库名就能用全部接口。

#### 4.3.2 核心流程

库文件之间的依赖关系（依据官方文档的库文件说明表）：

```text
用户程序 (example)
    │  -lasdsip（唯一必须显式链接的 SiP 库）
    ▼
libasdsip.so  /  libasdsip_static.a      ← 主用户库：聚合 utils/base/blas/fft/filter/interpolation
    │ 自动依赖（运行时由动态链接器解析）
    ▼
libasdsip_core.so                        ← 算子核心运行时：Ops 单例注册、kernel 加载调度、tiling
    │ 内部静态链接 libmki_static.a（发布模式）＋ CANN 算子编译框架
    ▼
libasdsip_host.so                        ← 主机端工具库（算子参数处理等）

第三方依赖（来自 CANN / MKI，需在库搜索路径中）：
libascendcl.so   ← ACL 运行时（aclTensor、设备/内存/流管理）
libaclnn.so      ← aclnn 算子库（fp16_t 等，BLAS 依赖）
libmki.so        ← MKI 框架（测试模式下动态链接）
```

用户视角的链路只有一条：**接口前缀 → 头文件 → `-lasdsip`**。

#### 4.3.3 源码精读

官方对照文档给出了权威结论：

[docs/header_files_library_files.md:L22-L30](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L22-L30) —— 头文件说明表逐行声明：`asdsip.h`、`base_api.h`、`fft_api.h`、`blas_api.h`、`filter_api.h`、`interp_api.h`、`domain/rs_api.h`，对应的库文件一栏**全部**是 `libasdsip.so 或 libasdsip_static.a`。

[docs/header_files_library_files.md:L38-L43](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/docs/header_files_library_files.md#L38-L43) —— 库文件表进一步说明分工：`libasdsip.so` 是「主用户库」，聚合全部模块；`libasdsip_core.so` 包含 Ops 单例、kernel 加载与 tiling，「由 libasdsip.so 自动依赖，用户通常无需单独引用」；`libasdsip_host.so` 是主机端辅助库。

官方示例脚本则展示了实战写法：

[example/build.sh:L30-L38](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh#L30-L38) —— 链接行依次给出 CANN 侧（`-lascendcl -lopapi -lnnopbase`）与 SiP 侧（`-lmki -lasdsip -lasdsip_core -lasdsip_host`）。注意这里比文档的「最小依赖」多链了三个库——示例脚本采取了保守的全显式链接策略；按文档说明，只 `-lasdsip` 理论上即可（其余由 so 的自动依赖解析），这正好构成 4.3.4 实践的验证点。

[example/build.sh:L28-L28](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/example/build.sh#L28-L28) —— 运行前把 `$ASDSIP_HOME_PATH/lib` 前置进 `LD_LIBRARY_PATH`，保证运行期动态链接器能找到这些 .so（承接 u1-l4 的环境变量知识）。

#### 4.3.4 代码实践

**实践目标**：用工具验证「一个主库聚合全部接口」——在 `libasdsip.so` 的符号表里找到来自不同模块的接口，并确认它对 `libasdsip_core.so` 的自动依赖。

**操作步骤**（在已安装 SiP 的环境执行）：

```bash
# 1. 三个不同模块的接口符号是否都在主库里？
nm -D ${ASDSIP_HOME_PATH}/lib/libasdsip.so | grep -E "asdFftCreate|asdBlasCreate|asdConvolve"

# 2. 主库自动依赖了哪些库？
ldd ${ASDSIP_HOME_PATH}/lib/libasdsip.so | grep -E "asdsip|ascendcl"

# 3. 最小链接实验：只链 -lasdsip 能否通过？（对照 example 的四库全链）
g++ tu_fft.cpp -I${ASDSIP_HOME_PATH}/include \
    -L${ASDSIP_HOME_PATH}/lib -lasdsip -o tu_fft_min
```

**需要观察的现象**：

1. 第 1 步预期三个前缀的符号都出现在 `libasdsip.so` 的动态符号表中——证明「六头一库」。
2. 第 2 步预期 `ldd` 输出里出现 `libasdsip_core.so => ...`——证明自动依赖真实存在，文档说法可验证。
3. 第 3 步若链接成功，说明最小依赖成立；若报 undefined reference，则说明该版本需要像 example 一样显式补链 `-lasdsip_core -lasdsip_host`，把这个实测结论记进笔记。

**预期结果**：三条命令都得到肯定证据，最小链接实验二选一（成功或需要补链）都是有效结论。所有输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：用户程序**必须**显式链接的 SiP 库最小集合是什么？静态库场景有什么额外要求？

**答案**：动态库场景最小集合就是 `-lasdsip`（`libasdsip.so`），`libasdsip_core.so` 由其自动依赖；若选静态库 `libasdsip_static.a`，文档要求额外链接 `libasdsip_core.so`（静态主库无法自动携带动态依赖）。

**练习 2**：`asdBlas*` 接口用到的 `fp16_t` 类型来自哪个软件包？缺失时会在什么阶段报错？

**答案**：来自 CANN 的 aclnn 头文件 `aclnn/opdev/fp16_t.h`（见 [include/blas_api.h:L14](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L14)），编译期需要 `-I${ASCEND_HOME_PATH}/include/aclnn`，运行期依赖 CANN 的 `libaclnn.so`/`libascendcl.so`；缺失时在**编译阶段**就报「找不到头文件」，而不是链接或运行阶段。

## 5. 综合实践

把本讲三块知识串成一份可长期使用的**《SiP API 地图速查表》**（建议存为自己的学习笔记文件）：

1. **表格生成**：通读 `include/` 下七个相关头文件，为每个模块补全一行：`前缀 | 头文件 | 风格（有柄/直调） | 代表接口 | 依赖的 -I 路径数 | 链接库`。Base 与 Domain 的前缀不规则（`swapLast2Axes`、`rs*`），请在表里如实标注。
2. **编译矩阵验证**：为六个模块各写一个只 include 对应头文件的最小翻译单元（4.2.4 已给出 3 个，补齐 `tu_base.cpp`、`tu_interp.cpp`、`tu_domain.cpp`），用 2×3 的结果矩阵记录「去掉某个 `-I` 后是否仍编译通过」，亲手复现依赖梯度：fft(1) < filter/base/interp/domain(2) < blas(3)。
3. **符号抽查**：每个模块挑 1 个接口名，用 `nm -D` 确认都能在 `libasdsip.so` 中找到，并对比 `asdFft*`（C 链接、未改编）与 `asdBlas*`（C++ 链接、改编）的符号形态差异。
4. **自查题**：合上笔记，随机抽 5 个接口名（可从 `example/A2/` 目录名里抽），写出所属模块、头文件、风格、是否需要 plan；全对才算本讲达标。

预期产出：一张六行速查表、一份 6×3 编译矩阵、一条符号形态结论。全部结论待本地验证后固化进笔记。

## 6. 本讲小结

- `asdsip.h` 是纯转发的伞形头文件，聚合且仅聚合六个模块头；判断公开 API 的唯一标准是「是否在这份聚合清单里」，`include/` 下的 `blas_common.h` 就是内部头的反例。
- 六大模块 = 两类风格：BLAS/FFT 走「句柄 + MakePlan + workspace + 同步」的有柄路线，Base/Filter/Interpolation/Domain 走「stream 直接传参、一次调用」的直调路线；直调家族的 `执行函数 + GetWorkspaceSize` 总是成对出现。
- BLAS 接口名可按公式解码：首位 S/C/H 编码 float/复数/半精度，I 前缀返回下标，尾缀 Batched 表示批量；MakePlan 的参数表就是该算子的静态形状清单。
- `fft_api.h` 是依赖最轻的公开头（前向声明 aclTensor、extern "C"、非强类型枚举），`blas_api.h` 依赖最重（还需 aclnn 头路径）——这决定了各自需要的 `-I` 数量。
- 六个头文件全部对应同一个主库 `libasdsip.so`（或静态库 `_static.a`）；`libasdsip_core.so`（Ops 单例、tiling、kernel 调度）由主库自动依赖，用户通常无需显式链接。

## 7. 下一步学习建议

- 下一讲 **u2-l2「Handle-Plan-Exec 编程模型」**：本讲只认识了接口的「长相」，下一讲按正确顺序把有柄风格的完整生命周期写成可运行骨架，解释 plan 复用的设计动机。
- 之后 **u2-l3「AspbStatus 返回码与错误处理」**：深入本讲反复出现的 `AspbStatus`，学会用校验宏写出规范的容错代码。
- 想提前浏览模块内部，建议按依赖从轻到重的顺序读：先重读 [include/fft_api.h](https://github.com/gitcode.com/cann/sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/fft_api.h#L46-L94) 的注释体会生命周期注释风格，再挑战最长的 [include/blas_api.h](https://github.com/gitcode.com/cann-sip/blob/da40fcab61b835839d9e89a479f6586152c219c0/include/blas_api.h#L34-L91)，尝试把每个 MakeXxxPlan 的参数归类为「形状类 / 模式类 / 步长类」。
- 头文件-库文件关系的权威文档是 `docs/header_files_library_files.md`，若日后版本变更，以它和 `asdsip.h` 实际内容为准复核本讲结论。
