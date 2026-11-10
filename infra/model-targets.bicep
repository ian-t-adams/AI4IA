@export()
func deploymentTarget(model object) string => model.?deploymentTarget ?? 'source'

@export()
func deploymentProvisioningEnabled(model object) bool => contains(model, 'provisioningEnabled') ? contains([true], model.?provisioningEnabled) : true

@export()
func deploymentRouting(model object) object? => model.?routing

@export()
func deploymentNameBase(model object) string => model.?deploymentNameBase ?? model.name
