from tilus.ir.layout import register_layout
from tilus.ir.layout.register_layout import visualize_layout

mma_c = register_layout(
    shape=[16, 8],
    mode_shape=[2, 8, 4, 2],
    spatial_modes=[1, 2],
    local_modes=[0, 3],
)
print("m16n8k8 C layout:")
print("  spatial_size =", mma_c.spatial_size)
print("  local_size   =", mma_c.local_size)
print("  size         =", mma_c.size)
print("  spatial_shape=", mma_c.spatial_shape)
print("  local_shape  =", mma_c.local_shape)
print()

big = register_layout(
    shape=[64, 64],
    mode_shape=[4, 2, 8, 8, 4, 2],
    spatial_modes=[0, 2, 4],
    local_modes=[1, 3, 5],
)
print("[64,64] / 128-thread layout:")
print("  spatial_size =", big.spatial_size)
print("  local_size   =", big.local_size)
print("  size         =", big.size)
print("  grouped_modes=", big.grouped_modes)
print("  mode_shape   =", big.mode_shape)
print("  spatial_modes=", big.spatial_modes)
print("  local_modes  =", big.local_modes)
print()

print("element (0,0): local =", big.get_local([0, 0]), " spatial =", big.get_spatial([0, 0]))
print("element (1,1): local =", big.get_local([1, 1]), " spatial =", big.get_spatial([1, 1]))
print()
mf = big.spatial_mfunction()
print("spatial_mfunction:", mf)
print("mf([0,0]) =", mf([0, 0]))
print()
print("=== visualize m16n8k8 (first rows) ===")
print(visualize_layout(mma_c)[:400])
