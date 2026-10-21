@export()
func deploymentTarget(model object) string => model.?deploymentTarget ?? 'source'

@export()
func deploymentRouting(model object) object? => model.?routing
