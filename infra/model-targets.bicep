@export()
func deploymentTarget(model object) string => model.?deploymentTarget ?? 'source'

@export()
// Property presence preserves legacy defaults; null must reach the Boolean return check.
#disable-next-line use-safe-access
func deploymentProvisioningEnabled(model object) bool => contains(model, 'provisioningEnabled') ? model.?provisioningEnabled : true

@export()
#disable-next-line use-safe-access
func deploymentRuntimeEnabled(model object) bool => contains(model, 'runtimeEnabled') ? model.?runtimeEnabled : true

@export()
func deploymentRouting(model object) object? => model.?routing

@export()
func deploymentNameBase(model object) string => model.?deploymentNameBase ?? model.name
