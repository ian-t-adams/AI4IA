@export()
func deploymentTarget(model object) string => model.?deploymentTarget ?? 'source'
