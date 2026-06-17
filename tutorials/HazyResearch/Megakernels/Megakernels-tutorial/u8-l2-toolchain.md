# 工具链与开发流程

本讲义介绍 Megakernels 项目的完整开发工具链，包括项目初始化、编译系统、权重加载和硬件配置检测等关键环节。掌握这些工具和流程是高效开发 megakernel 应用的基础。

## 最小模块 1：项目初始化工具

### 概念说明

Megakernels 项目提供了类似 `npm init` 的项目初始化工具，帮助开发者快速创建一个新的 megakernel 项目。这个工具解决了以下问题：

- **标准化项目结构**：确保所有项目遵循一致的目录结构和代码组织方式
- **减少重复工作**：自动生成样板代码和配置文件
- **降低入门门槛**：新开发者无需从零开始搭建项目

### 伪代码或流程

项目初始化流程如下：

```
1. 获取项目名称（通过命令行参数或交互式输入）
2. 验证项目名称（仅允许字母、数字、连字符和下划线）
3. 确定目标目录（默认为当前目录下的项目名子目录）
4. 创建基础目录结构（src/、tests/）
5. 处理模板文件：
   a. 读取模板文件内容
   b. 替换占位符（{{PROJECT_NAME}}、{{PROJECT_NAME_LOWER}}、{{PROJECT_NAME_UPPER}}）
   c. 写入目标文件
6. 创建 .gitignore 文件
7. 输出后续操作提示
```

### 原理分析

项目初始化工具的核心机制是**模板替换**。模板文件中使用占位符来标记需要替换的位置，工具在复制文件时动态替换这些占位符。

占位符规则：
- `{{PROJECT_NAME}}`：原始项目名称，保持大小写
- `{{PROJECT_NAME_LOWER}}`：项目名称转小写，用于文件名和变量名
- `{{PROJECT_NAME_UPPER}}`：项目名称转大写，用于宏定义和常量

目录结构设计遵循分离关注点原则：
- `src/`：存放 CUDA 源代码（.cu 文件）
- `tests/`：存放测试代码
- `setup.py`：Python 构建配置，用于编译 CUDA 扩展
- `README.md`：项目文档

### 代码实践

项目初始化工具的入口在 `util/mk_init/main.py`：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L66-L169

核心的占位符替换函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L23-L28

模板文件复制函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L30-L51

配置文件模板（`config.cuh`）定义了 megakernel 的关键参数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/config.cuh#L1-L48

基础的 CUDA 内核模板：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L1-L67

### 练习题

1. **模板扩展**：假设要添加一个新的配置参数 `BUFFER_SIZE`，应该在哪个模板文件中添加？如何使用占位符支持项目级别的自定义？

2. **目录结构**：如果项目需要包含 `benchmarks/` 目录用于性能测试，应该修改哪段代码？

3. **验证逻辑**：当前的名称验证只检查了字符是否为字母数字、连字符或下划线。如果要确保项目名称不以连字符开头，应该修改哪行代码？

4. **Git 集成**：如何在项目初始化后自动执行 `git init` 和 `git add .`？

### 答案

1. **模板扩展**：应该在 `util/mk_init/sources/src/config.cuh` 模板中添加。可以使用占位符如 `{{BUFFER_SIZE}}`，然后在 `main.py` 中添加相应的替换逻辑，或者通过环境变量让用户在编译时指定。

2. **目录结构**：应该修改 `create_project_structure` 函数，在 `directories` 列表中添加 `'benchmarks'`：
   ```python
   directories = [
       'src',
       'tests',
       'benchmarks',
   ]
   ```

3. **验证逻辑**：应该修改第 82-84 行的验证逻辑，添加额外检查：
   ```python
   if not project_name.replace('_', '').replace('-', '').isalnum():
       print("❌ Project name should only contain letters, numbers, hyphens, and underscores")
       sys.exit(1)
   if project_name.startswith('-') or project_name.startswith('_'):
       print("❌ Project name should not start with hyphen or underscore")
       sys.exit(1)
   ```

4. **Git 集成**：在 `main` 函数的最后，成功创建项目后添加：
   ```python
   import subprocess
   try:
       subprocess.run(['git', 'init'], cwd=target_dir, check=True, capture_output=True)
       subprocess.run(['git', 'add', '.'], cwd=target_dir, check=True, capture_output=True)
       print("✓ Initialized git repository")
   except subprocess.CalledProcessError:
       print("⚠ Failed to initialize git repository")
   ```

---

## 最小模块 2：编译系统

### 概念说明

Megakernels 项目使用 NVCC（NVIDIA CUDA Compiler）将 CUDA 代码编译为 Python 可加载的共享库（.so 文件）。编译系统解决了以下问题：

- **跨平台编译**：支持不同的 GPU 架构（4090、A100、H100、B200）
- **依赖管理**：正确链接 ThunderKittens、Megakernels 和 Python 运行时
- **优化配置**：启用关键的编译优化标志以获得最佳性能

### 伪代码或流程

编译流程（以 Makefile 为例）：

```
1. 检测环境变量：
   a. GPU 架构（GPU=H100/B200/4090/A100）
   b. Python 版本（PYTHON_VERSION=3.12）
   c. 依赖库路径（THUNDERKITTENS_ROOT、MEGAKERNELS_ROOT）

2. 根据 GPU 架构设置编译标志：
   if GPU == 4090:
       arch = sm_89
       define = KITTENS_4090
   elif GPU == A100:
       arch = sm_80
       define = KITTENS_A100
   elif GPU == H100:
       arch = sm_90a
       define = KITTENS_HOPPER
   else:  # B200
       arch = sm_100a
       define = KITTENS_HOPPER + KITTENS_BLACKWELL

3. 构建编译命令：
   nvcc [源文件] [优化标志] [架构标志] [包含路径] [链接库] -o [输出文件]

4. 执行编译，生成 Python 扩展模块
```

### 原理分析

编译系统包含多个关键组件：

**GPU 架构标志**：
- `-arch=sm_XX`：指定目标 GPU 的计算能力（Compute Capability）
  - `sm_89`：RTX 4090（Ada Lovelace）
  - `sm_80`：A100（Ampere）
  - `sm_90a`：H100（Hopper）
  - `sm_100a`：B200（Blackwell）

**优化标志**：
- `-O3`：最高优化级别
- `--use_fast_math`：使用快速数学函数（可能牺牲精度）
- `-Xptxas=--warn-on-spills`：寄存器溢出警告
- `--expt-extended-lambda`：支持扩展的 Lambda 表达式

**链接库**：
- Python 运行时（`-lpython{VERSION}`）
- CUDA 库（`-lcuda`、`-lcudart`、`-lcublas`）
- ThunderKittens 和 Megakernels 头文件

**Python 扩展**：
- 编译为共享库（`-shared -fPIC`）
- 使用 pybind11 暴露 C++ 函数给 Python

### 代码实践

Makefile 的核心编译逻辑：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L1-L42

GPU 架构的条件编译：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L20-L28

setup.py 中的编译配置（Python 扩展构建）：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L1-L138

自定义 CUDA 扩展构建类：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L90-L122

### 练习题

1. **架构选择**：如果要在 RTX 3090（sm_86）上编译，应该修改哪段代码？

2. **优化调试**：如何修改编译标志以生成带有调试符号（GDB 可用）的版本？

3. **依赖检测**：如何在编译前自动检测 `THUNDERKITTENS_ROOT` 和 `MEGAKERNELS_ROOT` 是否设置？

4. **多目标编译**：如何修改 Makefile 以支持一次编译多个 GPU 架构的目标？

### 答案

1. **架构选择**：在 Makefile 的 GPU 条件编译部分添加：
   ```makefile
   else ifeq ($(GPU),3090)
   NVCCFLAGS+= -DKITTENS_3090 -arch=sm_86
   ```
   或者在 setup.py 的条件判断中添加类似逻辑。

2. **优化调试**：移除优化标志并添加调试信息：
   ```makefile
   NVCCFLAGS=-g -G  # -g 生成调试信息，-G 生成设备端调试信息
   ```
   同时可以移除 `-O3` 和 `--use_fast_math`。

3. **依赖检测**：在 Makefile 开头添加检测逻辑：
   ```makefile
   ifndef THUNDERKITTENS_ROOT
   $(error THUNDERKITTENS_ROOT is not set)
   endif
   ifndef MEGAKERNELS_ROOT
   $(error MEGAKERNELS_ROOT is not set)
   endif
   ```

4. **多目标编译**：修改 Makefile 支持循环编译多个架构：
   ```makefile
   GPUS:=H100 B200
   all: $(GPUS)

   $(GPUS):
       $(MAKE) GPU=$@

   clean:
       for gpu in $(GPUS); do \
           rm -f $(TARGET)$(shell python3-config --extension-suffix); \
       done
   ```
   或者为每个架构生成独立的二进制文件：
   ```makefile
   $(TARGET)_%: $(SRC)
       $(NVCC) $(SRC) $(NVCCFLAGS) -DKITTENS_$* -arch=$(ARCH_$*) -o $(TARGET)_$*$(shell python3-config --extension-suffix)
   ```

---

## 最小模块 3：权重加载

### 概念说明

Megakernels 需要从磁盘加载预训练模型的权重参数。权重加载模块解决了以下问题：

- **格式兼容**：支持 Hugging Face safetensors 格式（安全且高效的权重存储格式）
- **张量并行**：支持将大模型分割到多个 GPU 上（Tensor Parallelism，TP）
- **选择性加载**：只加载当前计算需要的参数，节省内存

### 伪代码或流程

权重加载流程：

```
1. 确定权重文件路径：
   a. 检查是否存在 model.safetensors（单文件格式）
   b. 若不存在，检查 model.safetensors.index.json（分片格式）
   c. 从 index.json 中解析 weight_map，建立参数名到文件路径的映射

2. 筛选需要加载的参数：
   对于参数名 in weight_map:
       if 参数名 in include_parameters:
           添加对应文件到加载列表

3. 逐文件加载参数：
   for 文件 in 加载列表:
       打开 safetensors 文件
       for 键 in 文件:
           if 键 in include_parameters:
               if 需要张量并行 and 键在 tp_map 中:
                   获取当前张量的形状和分割维度
                   计算当前 rank 的分片边界
                   沿分割维度切片张量
                   state_dict[键] = 切片后的张量
               else:
                   state_dict[键] = 完整张量

4. 返回 state_dict
```

### 原理分析

**safetensors 格式**：
- 安全性：不像 pickle 可以执行任意代码
- 性能：使用内存映射（mmap）实现零拷贝加载
- 结构：每个文件包含多个张量，每个张量有元数据（形状、数据类型）

**张量并行（Tensor Parallelism）**：
- 将大矩阵沿某个维度分割到多个 GPU
- 例如：将 \( W \in \mathbb{R}^{d \times d} \) 沿列维分割到 4 个 GPU
  - GPU 0：\( W[:, 0:d/4] \)
  - GPU 1：\( W[:, d/4:d/2] \)
  - GPU 2：\( W[:, d/2:3d/4] \)
  - GPU 3：\( W[:, 3d/4:d] \)

**分片边界计算**：
给定张量形状 \( S \)、分割维度 \( dim \)、分片数 \( num\_shards \)、当前 rank \( shard\_index \)：

\[
base\_size = \left\lfloor \frac{S_{dim}}{num\_shards} \right\rfloor
\]
\[
remainder = S_{dim} \mod num\_shards
\]
\[
start = shard\_index \times base\_size + \min(shard\_index, remainder)
\]

如果 \( shard\_index < remainder \)：
\[
end = start + base\_size + 1
\]
否则：
\[
end = start + base\_size
\]

分片范围：\( slice(start, end) \)

### 代码实践

权重加载的核心函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py#L34-L97

分片边界计算函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py#L17-L31

张量并行切片逻辑：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py#L79-L93

### 练习题

1. **分片计算**：假设有一个形状为 `[4096, 4096]` 的张量，沿维度 0 分割到 3 个 GPU。计算 rank 0 和 rank 2 的分片边界。

2. **维度支持**：当前的 `load_safetensors_repo` 函数只支持维度 0 和 1 的分割。如果要支持维度 2 的分割，应该修改哪段代码？

3. **内存优化**：如果要进一步优化内存，可以在加载后立即将不需要的张量释放。如何在函数中实现这个逻辑？

4. **格式兼容**：除了 safetensors，Hugging Face 模型还常用 `.bin` 格式（PyTorch pickle）。如何扩展函数以支持这种格式？

### 答案

1. **分片计算**：
   - 张量形状：\( S = [4096, 4096] \)，分割维度：\( dim = 0 \)，分片数：\( num\_shards = 3 \)
   - \( base\_size = \lfloor 4096 / 3 \rfloor = 1365 \)
   - \( remainder = 4096 \mod 3 = 1 \)

   **rank 0**：
   - \( start = 0 \times 1365 + \min(0, 1) = 0 \)
   - \( 0 < 1 \)，所以 \( end = 0 + 1365 + 1 = 1366 \)
   - 分片：`[0:1366, :]`（大小 1366×4096）

   **rank 2**：
   - \( start = 2 \times 1365 + \min(2, 1) = 2730 + 1 = 2731 \)
   - \( 2 \not< 1 \)，所以 \( end = 2731 + 1365 = 4096 \)
   - 分片：`[2731:4096, :]`（大小 1365×4096）

2. **维度支持**：修改 match 分支，添加维度 2 的支持：
   ```python
   match split_dim:
       case 0:
           state_dict[k] = tensor_slice[shard_bounds]
       case 1:
           state_dict[k] = tensor_slice[:, shard_bounds]
       case 2:
           state_dict[k] = tensor_slice[:, :, shard_bounds]
       case _:
           raise ValueError(f"Unsupported split dimension: {split_dim}")
   ```

3. **内存优化**：在加载后立即删除不需要的引用：
   ```python
   for k in f.keys():
       if k in include_parameters:
           # ... 加载逻辑 ...
           if k not in include_parameters:  # 加载后检查
               del state_dict[k]
   ```
   或使用生成器逐个处理，而不是累积所有张量到 `state_dict`。

4. **格式兼容**：添加格式检测和 PyTorch 加载逻辑：
   ```python
   def load_weights(repo_path, include_parameters, device, ...):
       # 先尝试 safetensors
       safetensors_file = repo_path / "model.safetensors"
       if safetensors_file.exists():
           return load_safetensors_repo(...)

       # 再尝试 PyTorch .bin 格式
       bin_file = repo_path / "pytorch_model.bin"
       if bin_file.exists():
           import torch
           state_dict = torch.load(bin_file, map_location=device)
           return {k: v for k, v in state_dict.items() if k in include_parameters}

       raise FileNotFoundError("No supported weight format found")
   ```

---

## 最小模块 4：SM 数量检测

### 概念说明

 Streaming Multiprocessor（SM）是 NVIDIA GPU 上的核心计算单元。SM 数量检测解决了以下问题：

- **性能调优**：不同 GPU 的 SM 数量不同，需要根据硬件调整并行度
- **资源分配**：确定可以启动的最大线程块数
- **动态调度**：运行时根据 GPU 能力调整算法参数

### 伪代码或流程

SM 数量检测流程：

```
1. 获取 CUDA 设备属性：
   device_props = torch.cuda.get_device_properties(device)

2. 从属性中提取 SM 数量：
   sm_count = device_props.multi_processor_count

3. 返回 SM 数量
```

### 原理分析

**GPU 架构基础**：

GPU 由多个 SM 组成，每个 SM 包含：
- CUDA_cores：实际执行计算的单元
- 寄存器文件：存储线程本地数据
- 共享内存：线程块内共享的快速存储
- 调度器：调度 warp（32 个线程）执行

**常见 GPU 的 SM 数量**：
- RTX 4090（sm_89）：128 个 SM
- A100（sm_80）：108 或 128 个 SM（取决于型号）
- H100（sm_90a）：132 个 SM
- B200（sm_100a）：待确认

**使用场景**：

在 Megakernels 中，SM 数量用于：
- 确定注意力计算的分区数（`max_attn_partitions = get_sm_count(device)`）
- 调整线程网格大小（`dim3 grid(sm_count)`）
- 动态负载均衡

### 代码实践

SM 数量检测函数：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py#L107-L109

在调度器中的使用（确定注意力分区数）：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L51

在指令调度中的使用：

https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L52

### 练习题

1. **网格计算**：假设 GPU 有 128 个 SM，每个 SM 可以同时执行 16 个线程块。如果要最大化 GPU 利用率，应该启动多少个线程块？

2. **动态调整**：如何根据 SM 数量动态调整批量大小（batch size）以优化吞吐量？

3. **多 GPU 支持**：如何在多 GPU 环境中获取每个 GPU 的 SM 数量并计算总和？

4. **性能模型**：假设一个 kernel 需要 2 个 SM 来执行一个线程块，GPU 有 128 个 SM。理论上可以同时执行多少个线程块？

### 答案

1. **网格计算**：
   - 总线程块数 = \( SM\_count \times blocks\_per\_SM \)
   - \( 128 \times 16 = 2048 \) 个线程块
   - 但实际还要考虑寄存器和共享内存限制，可能需要减少

2. **动态调整**：
   ```python
   def calculate_batch_size(sm_count, base_batch=32):
       # 每个 SM 处理的样本数
       samples_per_sm = base_batch // 32  # 假设基准是 32 个 SM
       # 根据 SM 数量缩放
       return samples_per_sm * sm_count

   sm_count = get_sm_count(device)
   batch_size = calculate_batch_size(sm_count)
   ```

3. **多 GPU 支持**：
   ```python
   def get_total_sm_count(devices=None):
       if devices is None:
           devices = list(range(torch.cuda.device_count()))
       total_sm = 0
       for device in devices:
           total_sm += get_sm_count(f'cuda:{device}')
       return total_sm
   ```

4. **性能模型**：
   - 每个 block 需要 2 个 SM
   - 总 SM 数 = 128
   - 同时执行的 block 数 = \( 128 / 2 = 64 \) 个
   - 但由于 SM 分配的离散性和调度开销，实际可能略少

---

## 总结

本讲义介绍了 Megakernels 项目的核心工具链，包括：

1. **项目初始化工具**：快速创建标准化的项目结构
2. **编译系统**：支持多 GPU 架构的 CUDA 编译配置
3. **权重加载**：从 safetensors 格式加载模型权重，支持张量并行
4. **SM 数量检测**：获取 GPU 硬件信息用于性能调优

掌握这些工具和流程，开发者可以高效地构建、调试和优化 megakernel 应用。在实际开发中，建议：
- 使用项目初始化工具创建新项目，确保一致性
- 根据目标 GPU 修改编译标志
- 利用权重加载的选择性加载和张量并行特性
- 基于 SM 数量动态调整算法参数

下一步可以学习具体的 megakernel 实现细节（如注意力计算、矩阵乘法等）和性能优化技巧。
