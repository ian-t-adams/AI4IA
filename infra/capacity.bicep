// A missing production selection is a property-access error, never zero or baseline.
// The preprovision source validator checks the complete policy before ARM submission.
@export()
func selectedCapacity(deployment object, profile string) int => profile == 'production'
  ? deployment.production.capacity
  : profile == 'maximum' ? (deployment.?maxCapacity ?? deployment.capacity) : deployment.capacity
