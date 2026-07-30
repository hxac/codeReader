# 代码结构、核心 vs Ascend 分层与补丁机制

> 本讲承接 [u1-l1](u1-l1-project-overview-and-architecture.md)。在上一讲我们建立了「Triton-Ascend 是社区 Triton 的昇腾 NPU 可插拔后端」这张总导航图。本讲把镜头拉近，回答三个落地问题：**代码放在哪？谁属于核心、谁属于 Ascend？上游被「魔改」的部分怎么维护？**

## 1. 本讲目标

学完本讲，你应该能够：

- 在仓库里一眼分清「Triton core」与「Triton-Ascend」两部分代码，并说出判别依据。
- 画出 `third_party/ascend/` 下 `language / backend / lib / include / costmodel / patch` 等子目录各自的职责。
- 解释为什么上游 Triton 源文件（`python/`、`include/`、`lib/`、`bin/`）在仓库里保持「干净原貌」，而 Ascend 亲和改动以 **patch（补丁）** 形式交付。
- 区分两种补丁机制：**构建期** 的 `apply_triton_ascend_patch()`（`setup.py`）与 **运行期** 的 `_apply_ascend_patch()`（`third_party/ascend/backend/__init__.py`，由 `NPUOptions.__post_init__` 触发）。
- 独立在 patch 文件里找出真实的 Ascend 亲和修改，并判断一段代码的归属。

## 2. 前置知识

- **什么是 Triton / TTIR**：Triton 把 Python kernel 先翻译成与硬件无关的中间表示 TTIR，再交给「后端」翻译成具体硬件的二进制（详见 u1-l1）。本讲关心的是「仓库里这些代码到底怎么组织」。
- **什么是 patch（补丁）**：patch 是一种文本差异格式（`diff`/`git diff` 输出），描述「把文件 A 的第 N 行改成什么样」。`git apply foo.patch` 会按照这份差异就地修改文件。本讲的关键结论是：Triton-Ascend 没有直接改坏上游源文件，而是把改动写成 `.patch` 文件，在需要时再「贴」上去。
- **什么是 monkey-patch（运行期补丁）**：在程序运行时，用 Python 动态替换某个类/模块的属性（例如把 `CodeGenerator.__init__` 换成自己包装过的版本），从而在不改源码文件的前提下改变行为。这是「运行期补丁」的实现手段。
- **install 时如何安装 backend**：标准 Triton 规定后端放在 `third_party/<名字>/`，安装时会被「链接」到 `python/triton/backends/<名字>` 和 `python/triton/language/extra/<名字>`，从而被主程序发现。理解这一点才能看懂目录里那些「看起来重复」的链接关系。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 | 本讲用它说明什么 |
| --- | --- | --- |
| `docs/en/architecture_design_and_core_features.md` | 架构与代码结构官方说明 | 「核心 vs Ascend」分层原则与目录职责表 |
| `setup.py` | 构建/打包入口 | 构建期补丁应用 `apply_triton_ascend_patch()`、安装时的 backend/language 链接 |
| `pyproject.toml` | 构建系统声明 | 构建依赖（cmake、ninja、pybind11）与代码风格配置 |
| `third_party/ascend/patch/triton-ascend-3.6.0.patch` | 主补丁文件 | 对 16 个上游文件的具体 Ascend 亲和修改 |
| `third_party/ascend/patch/triton-ascend-dev-3.6.0.patch` | 开发期补丁 | 对 `autotuner.py` 的少量修改 |
| `third_party/ascend/backend/__init__.py` | Ascend 后端入口 | 运行期 monkey-patch `_apply_ascend_patch()` |
| `third_party/ascend/backend/compiler.py` | 编译后端主逻辑 | `NPUOptions.__post_init__` 触发运行期补丁 |

---

## 4. 核心概念与源码讲解

### 4.1 顶层目录组织与「核心 vs Ascend」分层

#### 4.1.1 概念说明

打开仓库根目录，你会看到 `python/`、`include/`、`lib/`、`bin/`、`test/`、`cmake/`，以及一个 `third_party/`。前几个目录看起来「和 Triton 一模一样」——这不是巧合，它们 **就是** 社区 Triton 的源码；而所有昇腾相关的东西，都集中在 `third_party/ascend/`。

官方文档用一句话总结了这条判别准则：

[architecture_design_and_core_features.md:33-38](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/docs/en/architecture_design_and_core_features.md#L33-L38) 给出「代码结构原则」：

> - 如果改动是 **target independent（与目标硬件无关）** 的，应该留在 **Triton core** 部分（例如对语言、runtime 的通用修改）。
> - 如果改动是 **target affinitive（与目标硬件亲和）** 的，应该放在 **Triton-Ascend** 部分。

也就是说，本项目的分层不是「按文件夹随便分」，而是有一条语义标准：**和 NPU/CANN/BiSheng 绑定的，进 `third_party/ascend/`；任何后端都能受益的通用改动，进 `python/` 等核心目录。**

#### 4.1.2 核心流程

判别一段代码归属的决策流程（伪代码）：

```
看到一段 Triton 源码改动
├─ 它是否只在 Ascend NPU 上有意义？（涉及 CANN / BiSheng / Cube-Vector / UB 等）
│   ├─ 是 → 属于 Triton-Ascend（third_party/ascend/）
│   └─ 否 → 进入下一步
├─ 它是否对所有后端（NVIDIA / AMD / Ascend）都通用？
│   ├─ 是 → 属于 Triton core（python/、include/、lib/、bin/）
│   └─ 边界模糊 → 优先保留在 core，仅把硬件专属部分抽出
```

> **注意一个反直觉点**：本项目里 `python/triton/runtime/jit.py`、`python/triton/language/semantic.py` 这些「核心」文件 **确实被 Ascend 改过**——但改动并没有直接写进文件，而是以补丁形式存在。这正是本讲后半段的重点。

#### 4.1.3 源码精读

目录职责表见 [architecture_design_and_core_features.md:40-54](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/docs/en/architecture_design_and_core_features.md#L40-L54)。关键几行翻译如下：

| 目录 | 架构层 | 说明 |
| --- | --- | --- |
| `python/` | Triton core | 标准 Triton 的通用 Python 实现（语言、JIT、runtime、cache、工具入口）。与目标无关的能力应优先放在这里 |
| `include/`、`lib/` | Triton core | 标准 Triton 的 C++/MLIR 基础设施、方言、pass、转换逻辑。Ascend 专属后端代码不放这里 |
| `third_party/ascend/` | Triton-Ascend | Ascend 后端根目录，含 NPU/CANN/BiSheng 专属的语言扩展、编译后端、runtime 驱动、MLIR pass、示例与测试 |
| `third_party/ascend/language/` | 语言扩展 | 安装时被链接到 `triton.language.extra`，使 kernel 可用 `triton.language.extra.cann` |
| `third_party/ascend/backend/compiler.py` | compiler | Ascend 编译后端主入口，注册编译选项、组织 TTIR lowering 各阶段 |

构建系统层面，`setup.py` 在第 765 行一次性声明了三个内置后端，`ascend` 排在第一个：

[setup.py:765](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L765) —— `backends = [*BackendInstaller.copy(["ascend", "nvidia", "amd"]), *BackendInstaller.copy_externals()]`

而打包目录映射 `get_package_dirs()` 的第一条就是 `("", "python")`，即整个 `python/` 作为包根，随后才追加各后端目录：

[setup.py:768-792](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L768-L792) —— 先 `yield ("", "python")`，再为每个 backend 产出 `triton.backends.<name>` 与 `triton.language.extra.<x>` 映射。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`python/` 是干净核心、`third_party/ascend/` 是 Ascend 专属」。

**操作步骤**：

1. 用 `ls python/triton/` 列出核心包结构，你会看到 `compiler/`、`runtime/`、`language/`、`backends/` 等标准 Triton 目录。
2. 用 `ls third_party/ascend/` 列出 Ascend 后端结构，对比二者差异。
3. 用 `ls third_party/` 查看：除了 `ascend`，还有 `nvidia`、`amd`、`proton`、`f2reduce`——说明 Ascend 是与 NVIDIA/AMD 平级的「可插拔后端」。

**需要观察的现象**：`python/` 里看不到任何 `cann`/`npu`/`hacc` 字样；这些字样只出现在 `third_party/ascend/`。

**预期结果**：两个目录内容互补、不重叠，印证「核心 vs Ascend」分层。

#### 4.1.5 小练习与答案

**练习 1**：`python/triton/backends/` 这个目录在仓库里几乎为空（或只有少量文件），为什么安装后却能 `import triton.backends.ascend`？
**参考答案**：因为安装时 `setup.py` 的 `add_link_to_backends()` 把 `third_party/ascend/backend` 软链接到了 `python/triton/backends/ascend`（见 [setup.py:817-841](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L817-L841)）。源码在 `third_party/`，「安装态」在 `python/`，靠链接桥接。

**练习 2**：如果有人想给 Triton 加一个「所有后端都能用」的通用循环优化，应该放在哪个目录？
**参考答案**：放在 `lib/` 或 `python/`（Triton core），因为它 target-independent；只有 NPU 专属部分才进 `third_party/ascend/lib/`。

---

### 4.2 third_party/ascend 子目录职责地图

#### 4.2.1 概念说明

`third_party/ascend/` 是一个「自包含」的后端：它几乎具备一个完整编译后端所需的全部部件——语言扩展、编译器、驱动、MLIR pass、代价模型、示例、测试。理解它的子目录划分，就等于拿到了后续所有讲义的「目录索引」。

#### 4.2.2 核心流程

各子目录与后续讲义的对应关系：

```
third_party/ascend/
├── language/      → Ascend 语言扩展（u7 整章）
│   ├── cann/libdevice.py        数学函数封装
│   └── cann/extension/          custom_op / mem_ops / sync 等亲和算子
├── backend/       → 编译器 + 驱动 + 运行时（u3、u5、u9）
│   ├── compiler.py              AscendBackend / NPUOptions / pass 编排
│   ├── driver.py                NPUDriver / NPULauncher
│   ├── __init__.py              运行期 monkey-patch（本讲）
│   └── runtime/                 autotuner / costmodel / ubtuner
├── lib/           → Ascend 专属 MLIR pass 的 C++ 实现（u4、u8）
├── include/       → 上述 pass 的头文件 / Passes.td
├── costmodel/     → 编译期代价模型 AscendModel（u9-l3）
│   └── configs/ascend_910b.json 硬件 schema
├── patch/         → 上游 Triton 补丁（本讲重点）
├── AscendNPU-IR/  → AscendNPU IR 与 BiSheng 链路集成
├── bin/           → triton-mlir-opt 等工具
├── tutorials/     → 示例 kernel（u1-l4）
└── unittest/      → pytest 与 MLIR conversion 测试（u10-l4）
```

注意 `lib/` 与 `include/` 的目录名和仓库根下的 `lib/`、`include/` **同名但内容完全不同**：根下的属于 Triton core（通用方言），`third_party/ascend/lib/` 下的全是 Ascend 专属 pass（如 `TritonToLinalg`、`DynamicCVPipeline`、`AutoBlockify`）。

#### 4.2.3 源码精读

`third_party/ascend/` 顶层除了子目录，还有两个值得注意的文件：

- `ascend_ir.cc` / `triton_ascend.cc`：Ascend IR 的 pybind 绑定，供 `from triton._C.libtriton.ascend import ir as ascend_ir` 这类导入使用（见 [third_party/ascend/backend/__init__.py:22](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L22)）。
- `backend/name.conf`：声明后端的名字（`ascend`），标准 Triton 用它识别后端身份。

`costmodel/` 是较独立的子系统，配置文件 `configs/ascend_910b.json`、`configs/hardware_schema.json` 描述硬件参数，C++ 代价模型实现在 `lib/AscendModel/`（详见 u9-l3）。

#### 4.2.4 代码实践

**实践目标**：用一次 `ls` 建立子目录与职责的对应记忆。

**操作步骤**：执行 `ls third_party/ascend/lib/`，你会看到约 16 个 pass 目录（`TritonToLinalg`、`DynamicCVPipeline`、`AutoBlockify`、`TritonToStructured`…）。再执行 `ls third_party/ascend/include/`，会发现它与 `lib/` 目录名几乎一一对应——每个 pass 都有「头文件目录 + 实现目录」一对。

**需要观察的现象**：`lib/` 与 `include/` 的目录名集合高度重合。

**预期结果**：理解 Ascend pass 的代码组织是「声明在 `include/`、实现在 `lib/`」的标准 MLIR pass 布局。**待本地验证**：你环境里的目录列表数量。

#### 4.2.5 小练习与答案

**练习 1**：用户在 kernel 里写 `import triton.language.extra.cann as cann`，这个 `cann` 实际来自磁盘上哪个目录？
**参考答案**：来自 `third_party/ascend/language/cann/`，安装时被链接到 `triton.language.extra.cann`（见架构表 [architecture_design_and_core_features.md:47](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/docs/en/architecture_design_and_core_features.md#L47)）。

**练习 2**：为什么 `third_party/ascend/lib/TritonToLinalg/` 不放在仓库根的 `lib/` 下？
**参考答案**：因为它 target-affinitive（把 TTIR 转成 Ascend 亲和的 Linalg/方言算子），按分层原则必须留在 `third_party/ascend/`；根下 `lib/` 只放通用方言。

---

### 4.3 构建期补丁机制：保持上游干净，Ascend 改动以 patch 交付

#### 4.3.1 概念说明

这是本讲最核心的机制。社区 Triton 的某些行为对 Ascend 并不友好（例如强制 tensor 元素数必须是 2 的幂），Triton-Ascend 需要修改这些上游文件。但项目选择 **不直接改坏源文件**，而是：

1. 让 `python/`、`include/`、`lib/`、`bin/` 里的上游文件保持 **干净原貌**（和社区 Triton 一致）。
2. 把所有 Ascend 亲和修改写成补丁文件 `triton-ascend-3.6.0.patch`。
3. 在 **构建期**（`pip install` / 编译扩展时）由 `setup.py` 自动 `git apply` 这些补丁，把干净源码「临时」变成 Ascend 版本。

这样做的好处是：上游文件可读、可对比、易于跟随社区升级；所有「魔改」集中、可审计、可回退。

#### 4.3.2 核心流程

构建期补丁应用的时序：

```
pip install / python setup.py build_ext
        │
        ▼
CMakeBuild.run()                      # setup.py:493
  ├─ download_and_copy_dependencies()  # 下载 NVIDIA 工具链等
  └─ apply_triton_ascend_patch()       # ★ 应用 Ascend 补丁
        │
        ▼
apply_triton_ascend_patch()           # setup.py:981
  ├─ checkout_file(dev_patch_files)    # 先 git checkout 还原 autotuner.py 为干净态
  ├─ apply_patch(dev patch)            # git apply 开发期补丁
  ├─ checkout_file(patch_files)        # 再还原 16 个上游文件为干净态
  └─ apply_patch(主 patch)             # git apply 主补丁
        │
        ▼
随后 cmake / ninja 真正编译（此时源码已是「贴过补丁」的 Ascend 版本）
```

两个底层辅助函数：

- `apply_patch(path)`：调用 `git apply` 把补丁贴到工作区（[setup.py:965-971](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L965-L971)）。
- `checkout_file(files)`：调用 `git checkout --` 把指定文件还原成 git 里干净的版本（[setup.py:974-978](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L974-L978)）。

「先 checkout 再 apply」是为了保证补丁能干净贴上——即便上一次构建残留了已打补丁的文件，`checkout` 会先把它们抹平。

#### 4.3.3 源码精读

构建入口 `CMakeBuild.run()` 在做任何 cmake 工作之前，先调用补丁应用：

[setup.py:493-495](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L493-L495) —— `run()` 内 `download_and_copy_dependencies()` 紧跟 `apply_triton_ascend_patch()`，之后才进入 cmake 版本检查与编译。

补丁应用本体 `apply_triton_ascend_patch()` 列出了被改的 **全部上游文件**：

[setup.py:981-1009](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L981-L1009) —— 定义两个补丁路径与两组文件清单：

```python
patch_files = [                      # 主补丁覆盖的 16 个上游文件
    "CMakeLists.txt",
    "include/triton/Dialect/Triton/IR/TritonAttrDefs.td",
    "lib/Dialect/Triton/IR/Traits.cpp",
    "python/src/ir.cc",
    "python/triton/_utils.py",
    "python/triton/compiler/code_generator.py",
    "python/triton/compiler/compiler.py",
    "python/triton/compiler/errors.py",
    "python/triton/language/math.py",
    "python/triton/language/semantic.py",
    "python/triton/language/standard.py",
    "python/triton/runtime/interpreter.py",
    "python/triton/runtime/jit.py",
    "bin/RegisterTritonDialects.h",
    "bin/triton-opt.cpp",
    "bin/CMakeLists.txt",
]
dev_patch_files = ["python/triton/runtime/autotuner.py"]   # 开发期补丁只动这一个
```

这份清单本身就是一份「Ascend 改了上游哪里」的目录。注意它横跨 C++（`include/`、`lib/`、`bin/`）、构建脚本（`CMakeLists.txt`）和 Python（`python/triton/...`）三层。

**三个真实的 Ascend 亲和修改示例**（直接摘自主补丁）：

1. **放宽 tensor 元素数必须 2 的幂的约束**。社区 Triton 在 `Traits.cpp` 里强制校验元素数为 2 的幂，但 Ascend 的 tiling 经常产生非 2 幂的块。补丁把该校验注释掉：

   [third_party/ascend/patch/triton-ascend-3.6.0.patch:105-117](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/patch/triton-ascend-3.6.0.patch#L105-L117) —— 把 `if ((numElements & (numElements - 1)) != 0) ...` 四行用 `// FIXME:patched triton community` 注释掉。这是典型 target-affinitive 改动，故以补丁交付而非进 core。

2. **新增 Ascend 专属的 HF32 输入精度枚举**。社区 `TritonAttrDefs.td` 的 `TT_InputPrecisionAttr` 只有 TF32/TF32x3/IEEE/BF16x3/BF16x6，补丁插入 Ascend 的 `HF32` 并顺移后续枚举值：

   [third_party/ascend/patch/triton-ascend-3.6.0.patch:89-100](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/patch/triton-ascend-3.6.0.patch#L89-L100) —— 新增 `I32EnumAttrCase<"HF32", 3, "hf32">`。HF32 是 Ascend 矩阵单元支持的精度模式，NVIDIA/AMD 后端不需要，因此只能走补丁。

3. **CMake 注入安全编译选项与 LLVM 版本兼容**。补丁给根 `CMakeLists.txt` 加入 `safe_compile.cmake`、为 AscendNPU-IR 适配 LLVM 21/22 的兼容宏、以及覆盖率工具挂钩：

   [third_party/ascend/patch/triton-ascend-3.6.0.patch:1-83](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/patch/triton-ascend-3.6.0.patch#L1-L83) —— `include(${CMAKE_CURRENT_SOURCE_DIR}/safe_compile.cmake)` 与 `LLVM_MAJOR_VERSION_22_COMPATIBLE` 选项。这些是构建链对 Ascend 工具链的专门适配。

> 补充：`third_party/ascend/patch/` 下还有一个 `llvm_patch_f6ded0b.patch`（对 LLVM 本身的补丁）。`setup.py` 的 `get_llvm_patch_hash()` 会把所有 `llvm_patch_*.patch` 求哈希，拼进预编译 LLVM 包的文件名里（[setup.py:206-222](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/setup.py#L206-L222)），从而保证换补丁时能重新下载匹配的 LLVM。这是「补丁影响构建产物命名」的一个巧妙用法，但和上游 Triton 补丁是两件事。

#### 4.3.4 代码实践

**实践目标**：从补丁文件里独立找出三处 Ascend 亲和修改，并解释「为何用补丁而非内联」。

**操作步骤**：

1. 打开 [third_party/ascend/patch/triton-ascend-3.6.0.patch](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/patch/triton-ascend-3.6.0.patch)。
2. 用 `grep '^diff --git' third_party/ascend/patch/triton-ascend-3.6.0.patch` 列出全部被改文件（应与 4.3.3 的清单一致）。
3. 任选三处 `diff --git` 块（建议选上面讲到的 `Traits.cpp`、`TritonAttrDefs.td`、`CMakeLists.txt`），阅读其 `+`/`-` 行。
4. 对每处写一句话：改了什么 + 为什么这对 Ascend 必要但对其他后端不适用。

**需要观察的现象**：每个 hunk 都明确标注了上游文件路径与行号；所有改动都是「增量」而非整文件替换。

**预期结果**：三处修改分别对应「约束放宽」「新增硬件精度」「构建链适配」三类典型的 target-affinitive 改动。**待本地验证**：你环境里补丁的具体行号（补丁可能随版本微调）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接把 `Traits.cpp` 里的 2 的幂校验删掉、提交进 `lib/`，而要写成补丁？
**参考答案**：因为该项目要让 `lib/` 尽量与社区 Triton 保持一致，便于跟随上游升级、减少合并冲突；删校验是 Ascend 专属需求（target-affinitive），所以用补丁在构建期叠加，保持源文件干净。

**练习 2**：`dev_patch_files` 只含 `autotuner.py`，它和主补丁为何要分开？
**参考答案**：开发期补丁（`triton-ascend-dev-3.6.0.patch`）改动小且偏开发态（如把 autotune 的异常类型改成 `MLIRCompilationError`，见 [triton-ascend-dev-3.6.0.patch:1-22](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/patch/triton-ascend-dev-3.6.0.patch#L1-L22)），与发布版主补丁解耦，方便独立维护和回退。

---

### 4.4 运行期补丁机制：__post_init__ 里的 monkey-patch

#### 4.4.1 概念说明

构建期补丁改的是 **文件**（编译前贴上去）。但有些 Ascend 行为不适合改源码文件——例如往生成的 MLIR module 里塞一个 `#hacc.target` 属性、或扩展 `compiler.parse` 支持新的 IR 扩展名。这些更适合在 **运行时** 用 Python 的 monkey-patch 动态注入。

Triton-Ascend 把这套运行期补丁放在 `third_party/ascend/backend/__init__.py` 的 `_apply_ascend_patch()` 里，并在 `NPUOptions.__post_init__`（每次创建编译选项时）触发它。这样既不污染上游文件，又能保证只要走 Ascend 后端就一定生效。

#### 4.4.2 核心流程

运行期补丁的触发与作用：

```
用户调用 kernel（触发 Ascend 编译）
        │
        ▼
构造 NPUOptions(...)                     # compiler.py
  └─ NPUOptions.__post_init__()          # compiler.py:1113
        └─ from triton.backends.ascend import _apply_ascend_patch
           _apply_ascend_patch()         # __init__.py:27  ★ 运行期补丁
                │
        ┌───────┼────────────────────────────┐
        ▼       ▼                            ▼
替换 CodeGenerator.__init__   替换 compiler.parse    替换 TritonSemantic.dot
(注入 #hacc.target)           (支持 ttadapter/        (HF32 守卫 +
                              mlirbc/npubin)          max_num_imprecise_acc)
```

三处 monkey-patch 都用「`_xxx_patch_applied` 标志位」做 **幂等保护**——多次调用只会真正替换一次，避免反复嵌套包装。

#### 4.4.3 源码精读

触发点在 `NPUOptions.__post_init__` 的最开头，**先于** 任何字段派生（如 `compile_mode` 解析）：

[third_party/ascend/backend/compiler.py:1113-1116](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1113-L1116) —— `__post_init__` 第一行就 `from triton.backends.ascend import _apply_ascend_patch` 并调用它。这保证后续编译流程用到的是已打过运行期补丁的对象。

运行期补丁本体 `_apply_ascend_patch()` 包含三段独立的 monkey-patch（[third_party/ascend/backend/__init__.py:27-113](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L27-L113)）：

1. **给生成的 module 注入 `#hacc.target` 属性**：包装 `CodeGenerator.__init__`，在原初始化之后，依据 `options.arch` 用 `ascend_ir` 构造 `#hacc.target<"arch">` 并 `set_attr` 到 module 上（[third_party/ascend/backend/__init__.py:30-51](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L30-L51)）。`hacc` 是 Ascend 专属方言，社区 `CodeGenerator` 当然不会写它，所以只能运行期注入。

2. **扩展 `compiler.parse` 的扩展名**：社区 `parse` 只认 `ttir/llir/ptx/cubin` 等，补丁新增对 `ttadapter`、`mlirbc`、`npubin` 的识别（[third_party/ascend/backend/__init__.py:57-74](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L57-L74)）。这些是 Ascend 编译阶段产物（见 u3-l2 的 `add_stages`），社区 Triton 不存在。

3. **`tl.dot` 的 HF32 守卫与 imprecise_acc 处理**：包装 `TritonSemantic.dot`，当 `input_precision == "hf32"` 但输入不是 fp32 时静默回退默认精度，并把不支持的 `max_num_imprecise_acc` 强制置 None（[third_party/ascend/backend/__init__.py:80-113](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L80-L113)）。注意这里的 HF32 与 4.3.3 里补丁新增的 HF32 枚举是 **配套** 的：枚举由构建期补丁引入，语义守卫由运行期补丁实现。

> 为什么 `dot` 的 HF32 守卫用运行期补丁而不是构建期补丁？因为它依赖运行期的 dtype 判断（`lhs.dtype.is_fp32()`），且只是「在调用原函数前做参数修正」，逻辑薄、改源码收益小、用 monkey-patch 更轻量。

#### 4.4.4 代码实践

**实践目标**：确认运行期补丁确实在编译时被触发。

**操作步骤**：

1. 在 [third_party/ascend/backend/compiler.py:1113-1116](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1113-L1116) 的 `_apply_ascend_patch()` 调用后临时加一行 `print("[probe] ascend runtime patch applied")`（仅本地调试，勿提交）。
2. 运行任意一个 NPU kernel（如 u1-l4 的 vector-add）。
3. 观察输出；如果开启了 IR dump（`MLIR_ENABLE_DUMP=1`），查找生成的 module 是否带有 `#hacc.target<"...">`。

**需要观察的现象**：每次构造 `NPUOptions` 都会打印一次 probe（受幂等标志保护，实际替换只发生一次）；dump 出的 module 顶层带 `hacc.target` 属性。

**预期结果**：印证「运行期补丁由 `__post_init__` 触发、为 module 注入 Ascend 属性」。**待本地验证**：需有 NPU 环境才能真正跑 kernel；无环境时可只做「源码阅读型实践」——跟踪 `_apply_ascend_patch` 三段 patch 各替换了哪个符号。

#### 4.4.5 小练习与答案

**练习 1**：`_apply_ascend_patch()` 里的 `_ascend_patch_applied` 标志有什么用？去掉会怎样？
**参考答案**：保证幂等——只在首次调用时真正替换 `CodeGenerator.__init__`。去掉后，每次构造 `NPUOptions` 都会再包一层，导致 `_patched_cg_init` 无限嵌套调用、最终栈溢出。

**练习 2**：构建期补丁（`Traits.cpp` 注释 2 的幂校验）和运行期补丁（`dot` 的 HF32 守卫）各自适合解决哪类问题？
**参考答案**：构建期补丁适合改 **静态源码/编译期校验/构建链**（C++、CMake、TD 定义）；运行期补丁适合改 **运行时对象行为/依赖运行期数据的判断**（Python 对象方法、动态属性注入），且改动较薄、不希望污染上游文件时。

---

## 5. 综合实践

把本讲三件事（分层判别、构建期补丁、运行期补丁）串起来完成下面这个「代码归属鉴定」任务：

1. **找三处构建期 Ascend 亲和修改**：在 [triton-ascend-3.6.0.patch](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/patch/triton-ascend-3.6.0.patch) 中挑三个不同上游文件（建议一个 C++、一个 Python、一个构建脚本），各写一句「为什么必须以补丁而非内联维护」。
2. **找一处运行期补丁**：在 [third_party/ascend/backend/__init__.py:27-113](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/__init__.py#L27-L113) 里选一段 monkey-patch，说明它如果改用「构建期改源码」方式会有什么缺点。
3. **找一处 target-independent 代码**：在 `python/triton/` 下任选一个文件（如 `runtime/jit.py` 的某段通用逻辑），论证它属于 Triton core 而非 Ascend——给出「它对所有后端通用」的理由。
4. **画一张补丁时序图**：把 `pip install` 到「源码变成 Ascend 版本」的完整路径画出来，标注 `CMakeBuild.run` → `apply_triton_ascend_patch` → `checkout_file` → `apply_patch` → cmake 编译。

> 进阶（可选）：对比 `setup.py` 的 `apply_triton_ascend_patch()`（构建期、改文件）与 `_apply_ascend_patch()`（运行期、改对象），用一张表总结二者在「作用对象、触发时机、可逆性、适用场景」上的差异。

## 6. 本讲小结

- 仓库按 **target-independent → Triton core（`python/`、`include/`、`lib/`、`bin/`），target-affinitive → Triton-Ascend（`third_party/ascend/`）** 的语义标准分层，不是随便分目录。
- `third_party/ascend/` 是自包含后端，`language/backend/lib/include/costmodel/patch` 等子目录分别承载语言扩展、编译器/驱动、MLIR pass、代价模型与上游补丁。
- 上游 Triton 源文件在仓库里保持 **干净原貌**；Ascend 亲和改动集中写在 `third_party/ascend/patch/triton-ascend-3.6.0.patch`，覆盖 16 个上游文件 + 1 个开发期文件。
- **构建期补丁** 由 `setup.py` 的 `apply_triton_ascend_patch()`（在 `CMakeBuild.run()` 中、cmake 之前）通过 `git checkout` + `git apply` 应用，便于跟随上游升级、可审计、可回退。
- **运行期补丁** 由 `third_party/ascend/backend/__init__.py` 的 `_apply_ascend_patch()` 实现，在 `NPUOptions.__post_init__` 触发，用 monkey-patch 注入 `hacc.target`、扩展 `parse`、给 `dot` 加 HF32 守卫，且带幂等保护。
- 典型 Ascend 亲和改动示例：放宽「tensor 元素数必须 2 的幂」校验、新增 HF32 输入精度枚举、CMake 注入安全编译与 LLVM 版本兼容。

## 7. 下一步学习建议

- 想看补丁具体怎么影响编译流程 → 下一讲 [u1-l3 安装与构建](u1-l3-installation-and-build.md) 会完整走一遍 `pip install` 与 `setup.py` 构建链。
- 想理解 `NPUOptions` 与 `AscendBackend` 如何组织编译阶段 → 直接跳到 u3-l2、u3-l3。
- 想看 Ascend 专属 MLIR pass 如何落地 → 进入 u4（pass 流水线）与 u8（Cube-Vector 融合）。
- 建议在本地把本讲的「代码归属鉴定」做一遍，再继续后续讲义——它能帮你后续读任何文件时迅速定位「这段代码归谁、由谁维护」。
